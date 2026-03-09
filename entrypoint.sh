#!/bin/bash
set -e

MODE="${1:-serve}"

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
    python /app/wp2static.py "$XML_FILE" -o /data/output
}

if [ "$MODE" = "export" ]; then
    run_export
    exit 0
fi

# Serve mode (default)
if [ ! -f /data/output/index.html ]; then
    echo "No static site found, running export first..."
    run_export
else
    echo "Static site found at /data/output/, skipping export."
fi

# Link output to nginx html dir
rm -rf /usr/share/nginx/html/*
ln -sf /data/output/* /usr/share/nginx/html/

echo "Starting nginx on port 80..."
exec nginx -g 'daemon off;'
