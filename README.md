# wp2static

Convert any WordPress WXR export to a fully static HTML site.

![screenshot](screenshot.png)

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
python wp2static.py <input.xml> [-o output_dir] [--skip-images]
python wp2static.py --help-export
```

| Flag | Description |
|------|-------------|
| `<input.xml>` | Path to WXR XML file (required) |
| `-o / --output` | Output directory (default: `./output`) |
| `--skip-images` | Skip downloading images |
| `--help-export` | Print WordPress export instructions |

## Docker usage

### Pull from GHCR

```bash
docker pull ghcr.io/xlith/wp2static:latest
```

### Build locally

```bash
docker build -t wp2static .
```

### Export + serve (default)

Exports the site (if not already done) then serves it via nginx on port 80:

```bash
docker run --rm -p 8080:80 \
  -v /path/to/export.xml:/data/export.xml \
  ghcr.io/xlith/wp2static
```

Open http://localhost:8080

### Export only (no server)

```bash
docker run --rm \
  -v /path/to/export.xml:/data/export.xml \
  -v $(pwd)/output:/data/output \
  ghcr.io/xlith/wp2static export
```

Static files will appear in `./output/`.

## Docker Compose

For persistent homelab setups:

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

On first run, the exporter converts the XML. On subsequent restarts, it detects the existing `index.html` and skips straight to serving.

## Output structure

```
output/
├── index.html
├── style.css
├── report.txt
├── posts/*.html
├── pages/*.html
└── images/wp-content/uploads/...
```

## How it works

- Parses the WXR XML and auto-detects site title, language, description, and image domains
- Downloads all images referenced in post/page content
- Generates clean HTML pages styled with [Bulma](https://bulma.io) CSS framework (CDN) and a minimal `style.css` for custom overrides
- Rewrites image URLs to local relative paths
- Produces an index page listing all posts and pages

## License

MIT
