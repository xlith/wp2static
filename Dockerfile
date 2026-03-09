FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/xlith/wp2static"

RUN apt-get update && \
    apt-get install -y --no-install-recommends nginx && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests

COPY wp2static.py /app/wp2static.py
COPY style.css /app/style.css
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

RUN mkdir -p /data/output

EXPOSE 80

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
