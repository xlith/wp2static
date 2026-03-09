# wp2static

Convert any WordPress WXR export to a fully static HTML site. No database, no PHP, no server-side dependencies — just HTML, CSS, and optionally a small filtering script.

## How to export from WordPress

1. Log in to your WordPress admin panel
2. Go to **Tools > Export** (direct URL: `https://YOUR-SITE/wp-admin/export.php`)
3. Select **All content**
4. Click **Download Export File**
5. You'll get a `.xml` file - that's your WXR export

## Quick start (Python)

Requires Python 3.8+ and `requests`:

```bash
pip install requests
python wp2static.py your-export.xml -o ./output
```

Open `output/index.html` in your browser.

### CLI options

```
python wp2static.py <input.xml> [-o output_dir] [--skip-images] [--download-only] [--plain] [-v]
python wp2static.py --help-export
```

| Flag | Description |
|------|-------------|
| `<input.xml>` | Path to WXR XML file (required) |
| `-o / --output` | Output directory (default: `./output`) |
| `--skip-images` | Generate HTML only, skip downloading images |
| `--download-only` | Only download missing images, skip HTML generation |
| `--plain` | Generate plain HTML without any CSS or JavaScript |
| `-v / --verbose` | Enable verbose logging (log every item, image, and file) |
| `--help-export` | Print WordPress export instructions |

### Typical workflows

```bash
# Full export (HTML + images)
python wp2static.py export.xml -o ./output

# Generate HTML first, download images later
python wp2static.py export.xml -o ./output --skip-images
python wp2static.py export.xml -o ./output --download-only

# Re-run to retry failed image downloads (already downloaded images are skipped)
python wp2static.py export.xml -o ./output --download-only
```

## Docker usage

### Pull from GHCR

```bash
docker pull ghcr.io/xlith/wp2static:latest
```

Multi-arch image: works natively on both `amd64` and `arm64` (Apple Silicon).

### Build locally

```bash
docker build -t wp2static .
```

### Export + serve (default)

Exports the site then serves it via nginx on port 80:

```bash
docker run --rm -p 8080:80 \
  -v /path/to/export.xml:/data/export.xml \
  -v $(pwd)/output:/data/output \
  ghcr.io/xlith/wp2static
```

Open http://localhost:8080

Extra flags are passed through to `wp2static.py` after the mode argument:

```bash
# Serve with verbose logging and skip images
docker run --rm -p 8080:80 \
  -v /path/to/export.xml:/data/export.xml \
  -v $(pwd)/output:/data/output \
  ghcr.io/xlith/wp2static serve --skip-images -v
```

### Export only (no server)

```bash
docker run --rm \
  -v /path/to/export.xml:/data/export.xml \
  -v $(pwd)/output:/data/output \
  ghcr.io/xlith/wp2static export
```

```bash
# Export HTML only, then download images later
docker run --rm \
  -v /path/to/export.xml:/data/export.xml \
  -v $(pwd)/output:/data/output \
  ghcr.io/xlith/wp2static export --skip-images

docker run --rm \
  -v /path/to/export.xml:/data/export.xml \
  -v $(pwd)/output:/data/output \
  ghcr.io/xlith/wp2static export --download-only
```

Static files will appear in `./output/`.

## Docker Compose

```yaml
services:
  wp2static:
    image: ghcr.io/xlith/wp2static:latest
    # build: .  # uncomment to build locally instead
    container_name: wp2static
    ports:
      - "8080:80"
    volumes:
      - ./my-export.xml:/data/export.xml
      - wp2static-data:/data/output
    restart: unless-stopped

volumes:
  wp2static-data:
```

```bash
docker compose up -d
```

The exporter runs on every container start to ensure the output is up to date. Already-downloaded images are skipped automatically.

## Output structure

```
output/
├── index.html          # Index with search and filters
├── style.css           # Self-contained stylesheet (no CDN)
├── report.txt          # Export summary and failed downloads
├── posts/*.html        # Individual post pages
├── pages/*.html        # Individual static pages
└── images/             # Downloaded images from wp-content/uploads
```

## How it works

1. Parses the WXR XML and auto-detects site title, language, description, and domains
2. Extracts all posts, pages, categories, comments, and attachments
3. Downloads images referenced in content (deduplicates by normalized URL)
4. Rewrites image URLs and internal links to local relative paths
5. Generates static HTML with a self-contained `style.css` (no CDN, no frameworks)
6. Index page includes search and filter controls (type, status, category, author)
7. `--plain` mode outputs pure HTML with zero CSS or JS dependencies

## License

MIT
