#!/bin/bash
set -e

MODE="${1:-serve}"

# Shift the first arg (mode) so "$@" contains only extra flags for wp2static
if [ "$#" -gt 0 ]; then
    shift
fi

find_xml() {
    XML_FILE=$(find /data -maxdepth 1 -name "*.xml" -type f | head -1)
    if [ -z "$XML_FILE" ]; then
        echo "Error: No .xml file found in /data/"
        echo "Mount your WXR export: -v /path/to/export.xml:/data/export.xml"
        exit 1
    fi
    echo "Found WXR file: $XML_FILE"
}

run_export() {
    find_xml
    echo "Running wp2static exporter..."
    if [ "$#" -gt 0 ]; then
        echo "  Extra flags: $*"
    fi
    python /app/wp2static.py "$XML_FILE" -o /data/output "$@"
}

if [ "$MODE" = "export" ]; then
    run_export "$@"
    exit 0
fi

# Serve mode (default): always re-export to pick up changes
run_export "$@"

# Point nginx root to output directory
sed -i 's|root /var/www/html|root /data/output|' /etc/nginx/sites-enabled/default

echo "Starting nginx on port 80..."
exec nginx -g 'daemon off;'
