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

    # Collect domains from channel link, base_site_url, attachment URLs, guids
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

    # Scan attachments and guids for additional domains
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

    # Always match localhost
    patterns.append(
        re.compile(r'http://localhost(?::\d+)?/wp-content/uploads/[^\s"\'<>]+', re.IGNORECASE)
    )

    # Generic fallback: any URL with /wp-content/uploads/
    patterns.append(
        re.compile(r'https?://[^\s"\'<>]+/wp-content/uploads/[^\s"\'<>]+', re.IGNORECASE)
    )

    return patterns


def build_normalize_url_func(domains):
    """Build a function that normalizes localhost/variant URLs to a downloadable domain."""
    # Pick the first non-localhost domain as the canonical download domain
    canonical_domain = None
    for domain in sorted(domains):
        if domain not in ("localhost", "127.0.0.1"):
            canonical_domain = domain
            break

    def normalize_image_url(url):
        if not canonical_domain:
            return url
        # Replace localhost URLs with canonical domain
        normalized = re.sub(
            r'http://localhost(?::\d+)?/wp-content/uploads/',
            f'https://{canonical_domain}/wp-content/uploads/',
            url,
        )
        return normalized

    return normalize_image_url


def parse_items(tree):
    """Parse all items from WXR XML, return posts, pages, and attachments."""
    channel = tree.find("channel")
    posts = []
    pages = []
    attachments = {}

    for item in channel.findall("item"):
        post_type = get_text(item, "wp:post_type")

        if post_type == "attachment":
            post_id = get_text(item, "wp:post_id")
            attachment_url = get_text(item, "wp:attachment_url")
            if post_id and attachment_url:
                attachments[post_id] = attachment_url
            continue

        if post_type not in ("post", "page"):
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

        if post_type == "post":
            posts.append(entry)
        else:
            pages.append(entry)

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

    unique_urls = sorted(set(all_urls.keys()))
    total = len(unique_urls)
    downloaded_count = 0
    skipped_count = 0

    print(f"\nDownloading {total} unique images...")

    for i, original_url in enumerate(unique_urls, 1):
        download_url = normalize_image_url(original_url)
        local_path = url_to_local_path(download_url)
        full_local_path = output_dir / local_path
        url_map[original_url] = str(local_path)

        if full_local_path.exists() and full_local_path.stat().st_size > 0:
            skipped_count += 1
            if i % 100 == 0 or i == total:
                print(f"  [{i}/{total}] Skipped (exists): {local_path.name}")
            continue

        full_local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            response = session.get(download_url, timeout=IMAGE_DOWNLOAD_TIMEOUT, stream=True)
            response.raise_for_status()
            with open(full_local_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            downloaded_count += 1
            if i % 50 == 0 or i == total:
                print(f"  [{i}/{total}] Downloaded: {local_path.name}")
        except Exception as e:
            error_msg = str(e)
            if hasattr(e, "response") and e.response is not None:
                error_msg = f"HTTP {e.response.status_code}: {e}"
            failed.append((original_url, error_msg, all_urls[original_url]))
            if i % 50 == 0 or i == total:
                print(f"  [{i}/{total}] FAILED: {local_path.name} - {error_msg}")

    print(f"\nImage download summary: {downloaded_count} downloaded, {skipped_count} skipped (existed), {len(failed)} failed")
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


# --- HTML Generation ---
BULMA_CDN = "https://cdn.jsdelivr.net/npm/bulma@1.0.4/css/bulma.min.css"

STATUS_CLASSES = {
    "draft": "is-warning is-light",
    "private": "is-danger is-light",
    "pending": "is-info is-light",
    "inherit": "is-light",
}


def copy_style_css(output_dir):
    source_path = Path(__file__).parent / "style.css"
    destination_path = output_dir / "style.css"
    shutil.copy2(source_path, destination_path)


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def status_badge(status):
    if status == "publish":
        return ""
    classes = STATUS_CLASSES.get(status, "is-light")
    return f' <span class="tag {classes}">{escape_html(status)}</span>'


def generate_item_html(entry, url_map, site_title, language, image_patterns):
    post_type = entry["post_type"]
    content = rewrite_image_urls(entry["content"], url_map, depth=1, image_patterns=image_patterns)

    categories_html = ""
    content_categories = [c for c in entry["categories"] if c["domain"] == "category"]
    if content_categories:
        tag_spans = "".join(f'<span class="tag is-info is-light">{escape_html(c["name"])}</span>' for c in content_categories)
        categories_html = f'<div class="tags">{tag_spans}</div>'

    comments_html = ""
    if entry["comments"]:
        comment_blocks = []
        for comment in entry["comments"]:
            comment_blocks.append(
                f'<div class="box comment-box">'
                f'<p><strong>{escape_html(comment["author"])}</strong> '
                f'<small class="has-text-grey">{escape_html(comment["date"])}</small></p>'
                f'<div>{comment["content"]}</div>'
                f'</div>'
            )
        comments_html = f'<h2 class="title is-4">Comments</h2>{"".join(comment_blocks)}'

    date_display = entry["post_date"][:10] if entry["post_date"] else ""
    title_escaped = escape_html(entry["title"])

    return f"""<!DOCTYPE html>
<html lang="{escape_html(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_escaped} - {escape_html(site_title)}</title>
<link rel="stylesheet" href="{BULMA_CDN}">
<link rel="stylesheet" href="../style.css">
</head>
<body>
<section class="section">
<div class="container">
<nav class="breadcrumb" aria-label="breadcrumbs">
<ul>
<li><a href="../index.html">{escape_html(site_title)}</a></li>
<li><a href="../index.html">{escape_html(post_type.title())}s</a></li>
<li class="is-active"><a href="#" aria-current="page">{title_escaped}</a></li>
</ul>
</nav>
<h1 class="title">{title_escaped}</h1>
<p class="subtitle is-6 has-text-grey">
{escape_html(date_display)} &middot; by {escape_html(entry["creator"] or "admin")} &middot; {escape_html(post_type)}{status_badge(entry["status"])}
</p>
{categories_html}
<div class="content">
{content}
</div>
{comments_html}
</div>
</section>
</body>
</html>"""


def generate_index_html(posts, pages, site_title, site_description, language):
    def item_row(entry, folder):
        slug = entry["post_name"] or entry["post_id"]
        href = f"{folder}/{slug}.html"
        date_str = entry["post_date"][:10] if entry["post_date"] else ""
        badge = status_badge(entry["status"])
        return (
            f'<a class="panel-block" href="{href}">'
            f'<span class="is-flex-grow-1">{escape_html(entry["title"])}{badge}</span>'
            f'<span class="item-date">{escape_html(date_str)}</span>'
            f'</a>'
        )

    sorted_pages = sorted(pages, key=lambda x: x["post_date"] or "", reverse=True)
    sorted_posts = sorted(posts, key=lambda x: x["post_date"] or "", reverse=True)

    pages_list = "\n".join(item_row(page, "pages") for page in sorted_pages)
    posts_list = "\n".join(item_row(post, "posts") for post in sorted_posts)

    subtitle_text = f"{escape_html(site_description)} &mdash; Static Export" if site_description else "Static Export"

    return f"""<!DOCTYPE html>
<html lang="{escape_html(language)}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape_html(site_title)}</title>
<link rel="stylesheet" href="{BULMA_CDN}">
<link rel="stylesheet" href="style.css">
</head>
<body>
<section class="hero is-link">
<div class="hero-body">
<div class="container">
<p class="title">{escape_html(site_title)}</p>
<p class="subtitle">{subtitle_text}</p>
</div>
</div>
</section>

<section class="section">
<div class="container">

<nav class="panel">
<p class="panel-heading">Pages ({len(pages)})</p>
{pages_list}
</nav>

<nav class="panel">
<p class="panel-heading">Posts ({len(posts)})</p>
{posts_list}
</nav>

</div>
</section>

<footer class="footer">
<div class="content has-text-centered">
<p>Generated by <strong>wp2static</strong></p>
</div>
</footer>
</body>
</html>"""


def write_report(posts, pages, image_stats, failed_images, output_dir, site_title):
    lines = [
        f"{'='*60}",
        f"  {site_title} - WordPress Export Report",
        f"{'='*60}",
        f"",
        f"Pages processed:  {len(pages)}",
        f"Posts processed:  {len(posts)}",
        f"Total items:      {len(pages) + len(posts)}",
        f"",
        f"Images found:       {image_stats['total']}",
        f"Images downloaded:  {image_stats['downloaded']}",
        f"Images skipped:     {image_stats['skipped']} (already existed)",
        f"Images failed:      {image_stats['failed_count']}",
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
    parser = argparse.ArgumentParser(
        prog="wp2static",
        description="Convert a WordPress WXR export to a static HTML site.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Export your WP site at: https://YOUR-SITE/wp-admin/export.php",
    )
    parser.add_argument("input", nargs="?", help="Path to WXR XML export file")
    parser.add_argument("-o", "--output", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--skip-images", action="store_true", help="Skip downloading images")
    parser.add_argument("--help-export", action="store_true", help="Show how to export from WordPress")

    args = parser.parse_args()

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

    xml_file = args.input
    output_dir = Path(args.output)

    if not os.path.isfile(xml_file):
        print(f"Error: file not found: {xml_file}")
        sys.exit(1)

    print(f"Parsing {xml_file}...")
    tree = ET.parse(xml_file)

    # Auto-detect site metadata
    metadata = detect_site_metadata(tree)
    site_title = metadata["title"]
    site_description = metadata["description"]
    language = metadata["language"]
    domains = metadata["domains"]

    print(f"Detected site: {site_title}")
    print(f"Language: {language}")
    print(f"Description: {site_description or '(none)'}")
    print(f"Domains found: {', '.join(sorted(domains)) or '(none)'}")

    # Build image URL patterns from discovered domains
    image_patterns = build_image_patterns(domains)
    normalize_image_url = build_normalize_url_func(domains)

    posts, pages, attachments = parse_items(tree)
    print(f"Found {len(posts)} posts, {len(pages)} pages, {len(attachments)} attachments")

    # Collect all image URLs
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

    # Create output directories
    (output_dir / "posts").mkdir(parents=True, exist_ok=True)
    (output_dir / "pages").mkdir(parents=True, exist_ok=True)
    (output_dir / "images").mkdir(parents=True, exist_ok=True)

    # Copy style.css to output
    copy_style_css(output_dir)

    # Download images
    url_map = {}
    failed_images = []
    image_stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed_count": 0}

    if args.skip_images:
        print("\nSkipping image downloads (--skip-images)")
        image_stats["total"] = len(all_image_urls)
    else:
        session = create_session()
        url_map, failed_images, image_stats = download_images(all_image_urls, session, output_dir, normalize_image_url)

    # Generate individual HTML pages
    print("\nGenerating HTML pages...")
    for entry in pages:
        slug = entry["post_name"] or entry["post_id"]
        filename = output_dir / "pages" / f"{slug}.html"
        html = generate_item_html(entry, url_map, site_title, language, image_patterns)
        filename.write_text(html, encoding="utf-8")

    print(f"  Generated {len(pages)} page files in {output_dir}/pages/")

    for entry in posts:
        slug = entry["post_name"] or entry["post_id"]
        filename = output_dir / "posts" / f"{slug}.html"
        html = generate_item_html(entry, url_map, site_title, language, image_patterns)
        filename.write_text(html, encoding="utf-8")

    print(f"  Generated {len(posts)} post files in {output_dir}/posts/")

    # Generate index
    index_html = generate_index_html(posts, pages, site_title, site_description, language)
    (output_dir / "index.html").write_text(index_html, encoding="utf-8")
    print(f"  Generated {output_dir}/index.html")

    # Report
    write_report(posts, pages, image_stats, failed_images, output_dir, site_title)


if __name__ == "__main__":
    main()
