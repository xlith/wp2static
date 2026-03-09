#!/usr/bin/env python3
"""wp2static - Convert any WordPress WXR export to a static HTML site."""

import argparse
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# WXR namespaces
NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "excerpt": "http://wordpress.org/export/1.2/excerpt/",
    "wp": "http://wordpress.org/export/1.2/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

IMAGE_DOWNLOAD_TIMEOUT = 30

BANNER = """
wp2static - WordPress to Static HTML Converter
───────────────────────────────────────────────
Tip: Export your WordPress site at https://YOUR-SITE/wp-admin/export.php
     or see: python wp2static.py --help-export
""".strip()

HELP_EXPORT_TEXT = """
How to export your WordPress site:

1. Log in to your WordPress admin panel
2. Go to Tools > Export
   Direct URL: https://YOUR-SITE/wp-admin/export.php
3. Select "All content"
4. Click "Download Export File"
5. You'll get a .xml file -- that's your WXR export

Then run:
  python wp2static.py your-export.xml -o ./output
""".strip()


# --- Logging ---
VERBOSE = False


def log(msg):
    """Print only in verbose mode."""
    if VERBOSE:
        print(msg)


def progress_bar(current, total, prefix="", width=40):
    """Render a terminal progress bar."""
    fraction = current / total if total else 1
    filled = int(width * fraction)
    bar = "█" * filled + "░" * (width - filled)
    percent = f"{fraction * 100:5.1f}%"
    print(f"\r  {prefix} [{bar}] {percent} ({current}/{total})", end="", flush=True)
    if current == total:
        print()


# --- HTTP Session with retry ---
def create_session():
    session = requests.Session()
    retry_strategy = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# --- XML Helpers ---
def get_text(element, tag, ns_map=NS):
    child = element.find(tag, ns_map)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def detect_site_metadata(tree):
    """Auto-detect site title, description, language, and domains from WXR."""
    channel = tree.find("channel")

    site_title = get_text(channel, "title") or "WordPress Site"
    site_description = get_text(channel, "description") or ""
    site_language = get_text(channel, "language") or "en"
    site_link = get_text(channel, "link") or ""

    domains = set()
    if site_link:
        parsed = urlparse(site_link)
        if parsed.hostname:
            domains.add(parsed.hostname)

    base_site_url = get_text(channel, "wp:base_site_url")
    if base_site_url:
        parsed = urlparse(base_site_url)
        if parsed.hostname:
            domains.add(parsed.hostname)

    base_blog_url = get_text(channel, "wp:base_blog_url")
    if base_blog_url:
        parsed = urlparse(base_blog_url)
        if parsed.hostname:
            domains.add(parsed.hostname)

    for item in channel.findall("item"):
        for tag in ["wp:attachment_url", "guid"]:
            url = get_text(item, tag)
            if url and "/wp-content/uploads/" in url:
                parsed = urlparse(url)
                if parsed.hostname:
                    domains.add(parsed.hostname)

    return {
        "title": site_title,
        "description": site_description,
        "language": site_language,
        "link": site_link,
        "domains": domains,
    }


def build_image_patterns(domains):
    """Build regex patterns for image URL detection from discovered domains."""
    patterns = []
    for domain in sorted(domains):
        escaped_domain = re.escape(domain)
        patterns.append(
            re.compile(rf'https?://(?:www\.)?{escaped_domain}/wp-content/uploads/[^\s"\'<>]+', re.IGNORECASE)
        )
    patterns.append(
        re.compile(r'http://localhost(?::\d+)?/wp-content/uploads/[^\s"\'<>]+', re.IGNORECASE)
    )
    patterns.append(
        re.compile(r'https?://[^\s"\'<>]+/wp-content/uploads/[^\s"\'<>]+', re.IGNORECASE)
    )
    return patterns


def build_normalize_url_func(domains):
    """Build a function that normalizes localhost/variant URLs to a downloadable domain."""
    canonical_domain = None
    for domain in sorted(domains):
        if domain not in ("localhost", "127.0.0.1"):
            canonical_domain = domain
            break

    def normalize_image_url(url):
        if not canonical_domain:
            return url
        return re.sub(
            r'http://localhost(?::\d+)?/wp-content/uploads/',
            f'https://{canonical_domain}/wp-content/uploads/',
            url,
        )

    return normalize_image_url


def parse_items(tree):
    """Parse all items from WXR XML, return posts, pages, and attachments."""
    channel = tree.find("channel")
    posts = []
    pages = []
    attachments = {}

    skipped_types = {}
    for item in channel.findall("item"):
        post_type = get_text(item, "wp:post_type")

        if post_type == "attachment":
            post_id = get_text(item, "wp:post_id")
            attachment_url = get_text(item, "wp:attachment_url")
            if post_id and attachment_url:
                attachments[post_id] = attachment_url
            continue

        if post_type not in ("post", "page"):
            skipped_types[post_type] = skipped_types.get(post_type, 0) + 1
            continue

        categories = []
        for cat_el in item.findall("category"):
            domain = cat_el.get("domain", "")
            cat_name = cat_el.text or ""
            if domain and cat_name:
                categories.append({"domain": domain, "name": cat_name, "nicename": cat_el.get("nicename", "")})

        comments = []
        for comment_el in item.findall("wp:comment", NS):
            comments.append({
                "author": get_text(comment_el, "wp:comment_author"),
                "date": get_text(comment_el, "wp:comment_date"),
                "content": get_text(comment_el, "wp:comment_content"),
                "approved": get_text(comment_el, "wp:comment_approved"),
            })

        entry = {
            "title": get_text(item, "title") or "(Untitled)",
            "content": get_text(item, "content:encoded"),
            "excerpt": get_text(item, "excerpt:encoded"),
            "post_id": get_text(item, "wp:post_id"),
            "post_date": get_text(item, "wp:post_date"),
            "post_name": get_text(item, "wp:post_name"),
            "status": get_text(item, "wp:status"),
            "post_type": post_type,
            "creator": get_text(item, "dc:creator"),
            "categories": categories,
            "comments": comments,
        }

        category_names = [c["name"] for c in categories if c["domain"] == "category"]
        cat_str = f" [{', '.join(category_names)}]" if category_names else ""
        comment_str = f" ({len(comments)} comments)" if comments else ""
        log(f"  {post_type.upper()}: {entry['title']} [{entry['status']}]{cat_str}{comment_str}")

        if post_type == "post":
            posts.append(entry)
        else:
            pages.append(entry)

    if skipped_types:
        log(f"  Skipped types: {', '.join(f'{t} ({n})' for t, n in sorted(skipped_types.items()))}")

    return posts, pages, attachments


# --- Image Downloading ---
def extract_image_urls(html_content, image_patterns):
    urls = set()
    for pattern in image_patterns:
        for match in pattern.findall(html_content):
            clean_url = match.rstrip(")")
            urls.add(clean_url)
    return urls


def url_to_local_path(url):
    parsed = urlparse(url)
    path = unquote(parsed.path).lstrip("/")
    return Path("images") / path


def download_images(all_urls, session, output_dir, normalize_image_url):
    url_map = {}
    failed = []

    # Deduplicate by normalized download URL so each file is only fetched once
    seen_targets = {}
    download_plan = []

    for original_url in sorted(all_urls.keys()):
        download_url = normalize_image_url(original_url)
        local_path = url_to_local_path(download_url)
        url_map[original_url] = str(local_path)

        if download_url not in seen_targets:
            seen_targets[download_url] = local_path
            download_plan.append((original_url, download_url, local_path))

    total = len(download_plan)
    downloaded_count = 0
    skipped_count = 0

    print(f"\nDownloading {total} unique images...")

    for i, (original_url, download_url, local_path) in enumerate(download_plan, 1):
        full_local_path = output_dir / local_path

        if full_local_path.exists() and full_local_path.stat().st_size > 0:
            skipped_count += 1
            log(f"  [{i}/{total}] Skipped (exists): {full_local_path}")
            progress_bar(i, total, prefix="Images")
            continue

        full_local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            log(f"  [{i}/{total}] Downloading: {download_url}")
            response = session.get(download_url, timeout=IMAGE_DOWNLOAD_TIMEOUT, stream=True)
            response.raise_for_status()
            with open(full_local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            file_size = full_local_path.stat().st_size
            downloaded_count += 1
            log(f"  [{i}/{total}] OK ({file_size:,} bytes): {local_path.name}")
        except Exception as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                error_msg = f"HTTP {e.response.status_code}: {e}"
            failed.append((original_url, error_msg, all_urls[original_url]))
            log(f"  [{i}/{total}] FAILED: {local_path.name} - {error_msg}")

        progress_bar(i, total, prefix="Images")

    print(f"  Summary: {downloaded_count} downloaded, {skipped_count} skipped, {len(failed)} failed")
    return url_map, failed, {"total": total, "downloaded": downloaded_count, "skipped": skipped_count, "failed_count": len(failed)}


def rewrite_image_urls(html_content, url_map, depth, image_patterns):
    prefix = "../" * depth

    def replacer(match):
        original_url = match.group(0).rstrip(")")
        extra = match.group(0)[len(original_url):]
        if original_url in url_map:
            return prefix + url_map[original_url] + extra
        return match.group(0)

    result = html_content
    for pattern in image_patterns:
        result = pattern.sub(replacer, result)
    return result


# --- Internal Link Rewriting ---
def build_slug_map(posts, pages):
    """Build a mapping from WP slugs to local static paths."""
    slug_map = {}
    for entry in pages:
        slug = entry["post_name"] or entry["post_id"]
        slug_map[slug] = f"pages/{slug}.html"
    for entry in posts:
        slug = entry["post_name"] or entry["post_id"]
        slug_map[slug] = f"posts/{slug}.html"
    return slug_map


def build_internal_link_patterns(domains):
    """Build regex patterns to match internal links."""
    patterns = []
    for domain in sorted(domains):
        escaped_domain = re.escape(domain)
        patterns.append(
            re.compile(
                rf'href=["\']https?://(?:www\.)?{escaped_domain}(/[^\s"\'<>]*)["\']',
                re.IGNORECASE,
            )
        )
    patterns.append(
        re.compile(
            r'href=["\']http://localhost(?::\d+)?(/[^\s"\'<>]*)["\']',
            re.IGNORECASE,
        )
    )
    return patterns


def rewrite_internal_links(html_content, slug_map, internal_link_patterns, depth):
    """Rewrite internal WP links to local static paths."""
    prefix = "../" * depth
    rewrites = 0

    def replacer(match):
        nonlocal rewrites
        path = match.group(1)
        clean_path = path.strip("/")
        if clean_path in slug_map:
            rewrites += 1
            return f'href="{prefix}{slug_map[clean_path]}"'
        segments = [s for s in clean_path.split("/") if s]
        if segments:
            last_segment = segments[-1]
            if "." in last_segment and not last_segment.endswith(".html"):
                return match.group(0)
            if last_segment in slug_map:
                rewrites += 1
                return f'href="{prefix}{slug_map[last_segment]}"'
        return match.group(0)

    result = html_content
    for pattern in internal_link_patterns:
        result = pattern.sub(replacer, result)
    return result, rewrites


# --- HTML Generation ---
STATUS_TAG_CLASSES = {
    "draft": "tag-draft",
    "private": "tag-private",
    "pending": "tag-pending",
}


def copy_style_css(output_dir):
    source_path = Path(__file__).parent / "style.css"
    destination_path = output_dir / "style.css"
    shutil.copy2(source_path, destination_path)


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def status_badge(status, plain=False):
    if status == "publish":
        return ""
    if plain:
        return f" [{status}]"
    tag_class = STATUS_TAG_CLASSES.get(status, "tag-default")
    return f' <span class="tag {tag_class}">{escape_html(status)}</span>'


def generate_item_html(entry, url_map, site_title, language, image_patterns, slug_map, internal_link_patterns, plain=False):
    post_type = entry["post_type"]
    content = rewrite_image_urls(entry["content"], url_map, depth=1, image_patterns=image_patterns)
    content, link_rewrites = rewrite_internal_links(content, slug_map, internal_link_patterns, depth=1)
    if link_rewrites:
        log(f"    Rewrote {link_rewrites} internal link(s)")

    date_display = entry["post_date"][:10] if entry["post_date"] else ""
    title_escaped = escape_html(entry["title"])

    if plain:
        return _item_html_plain(entry, content, site_title, language, date_display, title_escaped)

    categories_html = ""
    content_categories = [c for c in entry["categories"] if c["domain"] == "category"]
    if content_categories:
        tags = "".join(f'<span class="tag tag-category">{escape_html(c["name"])}</span>' for c in content_categories)
        categories_html = f'<div class="categories">{tags}</div>'

    comments_html = ""
    if entry["comments"]:
        blocks = []
        for comment in entry["comments"]:
            blocks.append(
                f'<div class="comment">'
                f'<div class="comment-meta"><strong>{escape_html(comment["author"])}</strong> &middot; {escape_html(comment["date"])}</div>'
                f'<div>{comment["content"]}</div>'
                f'</div>'
            )
        comments_html = f'<h2>Comments</h2>{"".join(blocks)}'

    return f"""<!DOCTYPE html>
<html lang="{escape_html(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_escaped} - {escape_html(site_title)}</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
<div class="section">
<div class="container">
<div class="breadcrumb">
<a href="../index.html">{escape_html(site_title)}</a> <span>/</span>
<a href="../index.html">{escape_html(post_type.title())}s</a> <span>/</span>
{title_escaped}
</div>
<h1>{title_escaped}</h1>
<div class="meta">
{escape_html(date_display)} &middot; by {escape_html(entry["creator"] or "admin")} &middot; {escape_html(post_type)}{status_badge(entry["status"])}
</div>
{categories_html}
<div class="content">
{content}
</div>
{comments_html}
</div>
</div>
</body>
</html>"""


def _item_html_plain(entry, content, site_title, language, date_display, title_escaped):
    """Generate a plain HTML page with no CSS or JS."""
    post_type = entry["post_type"]
    categories = [c["name"] for c in entry["categories"] if c["domain"] == "category"]
    cat_str = ", ".join(escape_html(c) for c in categories)
    cat_line = f"<p><em>Categories: {cat_str}</em></p>" if cat_str else ""

    comments_html = ""
    if entry["comments"]:
        items = []
        for comment in entry["comments"]:
            items.append(
                f'<li><strong>{escape_html(comment["author"])}</strong> '
                f'({escape_html(comment["date"])})<br>{comment["content"]}</li>'
            )
        comments_html = f'<h2>Comments</h2><ul>{"".join(items)}</ul>'

    status_str = f" [{entry['status']}]" if entry["status"] != "publish" else ""

    return f"""<!DOCTYPE html>
<html lang="{escape_html(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_escaped} - {escape_html(site_title)}</title>
</head>
<body>
<p><a href="../index.html">&larr; {escape_html(site_title)}</a></p>
<h1>{title_escaped}{status_str}</h1>
<p><small>{escape_html(date_display)} | by {escape_html(entry["creator"] or "admin")} | {escape_html(post_type)}</small></p>
{cat_line}
<hr>
{content}
{comments_html}
</body>
</html>"""


def generate_index_html(posts, pages, site_title, site_description, language, plain=False):
    if plain:
        return _index_html_plain(posts, pages, site_title, site_description, language)

    all_categories = set()
    all_statuses = set()
    all_authors = set()
    for entry in posts + pages:
        all_statuses.add(entry["status"])
        all_authors.add(entry["creator"] or "admin")
        for cat in entry["categories"]:
            if cat["domain"] == "category":
                all_categories.add(cat["name"])

    def item_row(entry, folder):
        slug = entry["post_name"] or entry["post_id"]
        href = f"{folder}/{slug}.html"
        date_str = entry["post_date"][:10] if entry["post_date"] else ""
        badge = status_badge(entry["status"])
        entry_categories = [c["name"] for c in entry["categories"] if c["domain"] == "category"]
        data_cats = escape_html(",".join(entry_categories))
        data_status = escape_html(entry["status"])
        data_type = escape_html(entry["post_type"])
        data_author = escape_html(entry["creator"] or "admin")
        cat_tags = "".join(
            f'<span class="tag tag-category">{escape_html(c)}</span>'
            for c in entry_categories
        )
        return (
            f'<a class="item-row" href="{href}" '
            f'data-type="{data_type}" data-status="{data_status}" '
            f'data-category="{data_cats}" data-author="{data_author}">'
            f'<span class="item-title">{escape_html(entry["title"])}{badge}{cat_tags}</span>'
            f'<span class="item-date">{escape_html(date_str)}</span>'
            f'</a>'
        )

    sorted_pages = sorted(pages, key=lambda x: x["post_date"] or "", reverse=True)
    sorted_posts = sorted(posts, key=lambda x: x["post_date"] or "", reverse=True)

    all_items = [(p, "pages") for p in sorted_pages] + [(p, "posts") for p in sorted_posts]
    items_html = "\n".join(item_row(entry, folder) for entry, folder in all_items)

    subtitle_text = f"{escape_html(site_description)} &mdash; Static Export" if site_description else "Static Export"

    category_options = "".join(
        f'<option value="{escape_html(c)}">{escape_html(c)}</option>'
        for c in sorted(all_categories)
    )
    status_options = "".join(
        f'<option value="{escape_html(s)}">{escape_html(s)}</option>'
        for s in sorted(all_statuses)
    )
    author_options = "".join(
        f'<option value="{escape_html(a)}">{escape_html(a)}</option>'
        for a in sorted(all_authors)
    )

    return f"""<!DOCTYPE html>
<html lang="{escape_html(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape_html(site_title)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<div class="hero">
<div class="container">
<h1>{escape_html(site_title)}</h1>
<p>{subtitle_text}</p>
</div>
</div>

<div class="section">
<div class="container">

<div class="filter-bar">
<input type="text" id="search" placeholder="Search titles...">
<div class="filter-row">
<div class="filter-group">
<label>Type</label>
<select id="filter-type">
<option value="">All types</option>
<option value="page">Pages</option>
<option value="post">Posts</option>
</select>
</div>
<div class="filter-group">
<label>Status</label>
<select id="filter-status">
<option value="">All statuses</option>
{status_options}
</select>
</div>
<div class="filter-group">
<label>Category</label>
<select id="filter-category">
<option value="">All categories</option>
{category_options}
</select>
</div>
<div class="filter-group">
<label>Author</label>
<select id="filter-author">
<option value="">All authors</option>
{author_options}
</select>
</div>
</div>
</div>

<div class="content-list">
<div class="content-list-header">
All Content <span class="count" id="count-badge">{len(posts) + len(pages)}</span>
</div>
{items_html}
<div class="no-results" id="no-results">No items match your filters.</div>
</div>

</div>
</div>

<div class="footer">Generated by <strong>wp2static</strong></div>

<script>
(function() {{
  var search = document.getElementById('search');
  var filterType = document.getElementById('filter-type');
  var filterStatus = document.getElementById('filter-status');
  var filterCategory = document.getElementById('filter-category');
  var filterAuthor = document.getElementById('filter-author');
  var rows = document.querySelectorAll('.item-row');
  var noResults = document.getElementById('no-results');
  var countBadge = document.getElementById('count-badge');

  function applyFilters() {{
    var query = search.value.toLowerCase();
    var typeVal = filterType.value;
    var statusVal = filterStatus.value;
    var catVal = filterCategory.value;
    var authorVal = filterAuthor.value;
    var visible = 0;

    rows.forEach(function(row) {{
      var title = row.textContent.toLowerCase();
      var ok = (!query || title.indexOf(query) !== -1)
        && (!typeVal || row.dataset.type === typeVal)
        && (!statusVal || row.dataset.status === statusVal)
        && (!catVal || row.dataset.category.split(',').indexOf(catVal) !== -1)
        && (!authorVal || row.dataset.author === authorVal);

      row.style.display = ok ? '' : 'none';
      if (ok) visible++;
    }});

    countBadge.textContent = visible;
    noResults.style.display = visible === 0 ? 'block' : 'none';
  }}

  search.addEventListener('input', applyFilters);
  filterType.addEventListener('change', applyFilters);
  filterStatus.addEventListener('change', applyFilters);
  filterCategory.addEventListener('change', applyFilters);
  filterAuthor.addEventListener('change', applyFilters);
}})();
</script>
</body>
</html>"""


def _index_html_plain(posts, pages, site_title, site_description, language):
    """Generate a plain HTML index with no CSS or JS."""

    def item_li(entry, folder):
        slug = entry["post_name"] or entry["post_id"]
        href = f"{folder}/{slug}.html"
        date_str = entry["post_date"][:10] if entry["post_date"] else ""
        status_str = f" [{entry['status']}]" if entry["status"] != "publish" else ""
        date_part = f" <small>({date_str})</small>" if date_str else ""
        return f'<li><a href="{href}">{escape_html(entry["title"])}</a>{status_str}{date_part}</li>'

    sorted_pages = sorted(pages, key=lambda x: x["post_date"] or "", reverse=True)
    sorted_posts = sorted(posts, key=lambda x: x["post_date"] or "", reverse=True)

    pages_list = "\n".join(item_li(p, "pages") for p in sorted_pages)
    posts_list = "\n".join(item_li(p, "posts") for p in sorted_posts)

    desc = f"<p>{escape_html(site_description)}</p>" if site_description else ""

    return f"""<!DOCTYPE html>
<html lang="{escape_html(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape_html(site_title)}</title>
</head>
<body>
<h1>{escape_html(site_title)}</h1>
{desc}
<h2>Pages ({len(pages)})</h2>
<ul>
{pages_list}
</ul>
<h2>Posts ({len(posts)})</h2>
<ul>
{posts_list}
</ul>
<hr>
<p><small>Generated by wp2static</small></p>
</body>
</html>"""


def write_report(posts, pages, image_stats, failed_images, link_stats, output_dir, site_title):
    lines = [
        f"{'='*60}",
        f"  {site_title} - WordPress Export Report",
        f"{'='*60}",
        f"",
        f"Pages processed:    {len(pages)}",
        f"Posts processed:    {len(posts)}",
        f"Total items:        {len(pages) + len(posts)}",
        f"",
        f"Images found:       {image_stats['total']}",
        f"Images downloaded:  {image_stats['downloaded']}",
        f"Images skipped:     {image_stats['skipped']} (already existed)",
        f"Images failed:      {image_stats['failed_count']}",
        f"",
        f"Internal links rewritten: {link_stats}",
    ]

    if failed_images:
        lines.append("")
        lines.append(f"{'─'*60}")
        lines.append("FAILED IMAGE DOWNLOADS:")
        lines.append(f"{'─'*60}")
        for url, error, referencing in failed_images:
            lines.append(f"")
            lines.append(f"  URL:   {url}")
            lines.append(f"  Error: {error}")
            refs = ", ".join(referencing) if referencing else "unknown"
            lines.append(f"  Referenced by: {refs}")

    lines.append("")
    lines.append(f"{'='*60}")
    lines.append(f"Output directory: {output_dir.resolve()}")
    lines.append(f"{'='*60}")

    report_text = "\n".join(lines)
    print("\n" + report_text)

    report_path = output_dir / "report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\nReport saved to {report_path}")


# --- Main ---
def main():
    global VERBOSE

    parser = argparse.ArgumentParser(
        prog="wp2static",
        description="Convert a WordPress WXR export to a static HTML site.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Export your WP site at: https://YOUR-SITE/wp-admin/export.php",
    )
    parser.add_argument("input", nargs="?", help="Path to WXR XML export file")
    parser.add_argument("-o", "--output", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--skip-images", action="store_true", help="Skip downloading images (generate HTML only)")
    parser.add_argument("--download-only", action="store_true", help="Only download missing images, skip HTML generation")
    parser.add_argument("--plain", action="store_true", help="Generate plain HTML without any CSS or JS")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--help-export", action="store_true", help="Show how to export from WordPress")

    args = parser.parse_args()
    VERBOSE = args.verbose

    if args.help_export:
        print(HELP_EXPORT_TEXT)
        sys.exit(0)

    if not args.input:
        print(BANNER)
        print()
        parser.print_usage()
        print("\nError: the following argument is required: input")
        print("Run 'python wp2static.py --help' for full usage info.")
        sys.exit(1)

    print(BANNER)
    print()

    if args.skip_images and args.download_only:
        print("Error: --skip-images and --download-only cannot be used together.")
        sys.exit(1)

    plain = args.plain
    download_only = args.download_only

    if download_only:
        print("Mode: download missing images only (no HTML generation)")
    elif plain:
        print("Mode: plain HTML (no CSS, no JavaScript)")

    xml_file = args.input
    output_dir = Path(args.output)

    if not os.path.isfile(xml_file):
        print(f"Error: file not found: {xml_file}")
        sys.exit(1)

    file_size = os.path.getsize(xml_file)
    print(f"Parsing {xml_file} ({file_size:,} bytes)...")
    tree = ET.parse(xml_file)

    metadata = detect_site_metadata(tree)
    site_title = metadata["title"]
    site_description = metadata["description"]
    language = metadata["language"]
    domains = metadata["domains"]

    print(f"Detected site: {site_title}")
    log(f"  Language: {language}")
    log(f"  Description: {site_description or '(none)'}")
    print(f"Domains found: {', '.join(sorted(domains)) or '(none)'}")

    image_patterns = build_image_patterns(domains)
    normalize_image_url = build_normalize_url_func(domains)
    internal_link_patterns = build_internal_link_patterns(domains)

    print(f"\nParsing items...")
    posts, pages, attachments = parse_items(tree)
    print(f"Found {len(posts)} posts, {len(pages)} pages, {len(attachments)} attachments")

    slug_map = build_slug_map(posts, pages)
    log(f"  Built slug map with {len(slug_map)} entries")

    all_image_urls = {}
    for entry in posts + pages:
        if not entry["content"]:
            continue
        urls = extract_image_urls(entry["content"], image_patterns)
        for url in urls:
            if url not in all_image_urls:
                all_image_urls[url] = []
            all_image_urls[url].append(entry["title"])

    print(f"Found {len(all_image_urls)} unique image URLs in content")

    log(f"\nCreating output directories in {output_dir.resolve()}...")
    for subdir in ("posts", "pages", "images"):
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)
        log(f"  {output_dir / subdir}/")

    # --- Image downloading ---
    url_map = {}
    failed_images = []
    image_stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed_count": 0}

    if args.skip_images:
        print("\nSkipping image downloads (--skip-images)")
        image_stats["total"] = len(all_image_urls)
    else:
        session = create_session()
        url_map, failed_images, image_stats = download_images(all_image_urls, session, output_dir, normalize_image_url)

    if download_only:
        print(f"\n  Download-only mode: skipping HTML generation.")
        write_report(posts, pages, image_stats, failed_images, 0, output_dir, site_title)
        return

    # --- HTML generation ---
    if not plain:
        copy_style_css(output_dir)
        log(f"  Copied style.css")

    total_items = len(pages) + len(posts)
    total_link_rewrites = 0
    print(f"\nGenerating {total_items} HTML files...")
    item_counter = 0

    for entry in pages:
        slug = entry["post_name"] or entry["post_id"]
        filename = output_dir / "pages" / f"{slug}.html"
        html = generate_item_html(entry, url_map, site_title, language, image_patterns, slug_map, internal_link_patterns, plain=plain)
        filename.write_text(html, encoding="utf-8")
        log(f"  Page: {filename.name} ({entry['title']})")
        item_counter += 1
        progress_bar(item_counter, total_items, prefix="HTML  ")

    for entry in posts:
        slug = entry["post_name"] or entry["post_id"]
        filename = output_dir / "posts" / f"{slug}.html"
        html = generate_item_html(entry, url_map, site_title, language, image_patterns, slug_map, internal_link_patterns, plain=plain)
        filename.write_text(html, encoding="utf-8")
        log(f"  Post: {filename.name} ({entry['title']})")
        item_counter += 1
        progress_bar(item_counter, total_items, prefix="HTML  ")

    index_html = generate_index_html(posts, pages, site_title, site_description, language, plain=plain)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"  Index: {output_dir}/index.html")

    for entry in posts + pages:
        if entry["content"]:
            _, rewrites = rewrite_internal_links(entry["content"], slug_map, internal_link_patterns, depth=1)
            total_link_rewrites += rewrites

    if total_link_rewrites:
        print(f"  Rewrote {total_link_rewrites} internal link(s) across all content")

    write_report(posts, pages, image_stats, failed_images, total_link_rewrites, output_dir, site_title)


if __name__ == "__main__":
    main()
