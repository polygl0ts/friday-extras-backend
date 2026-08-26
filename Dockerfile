FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Run as a normal user rather than root. Nothing here needs privilege: the
# service listens on 8000 (unprivileged) and its state lives in Postgres.
#
# `/data` is created and chowned here on purpose. The production database is
# Postgres over the network, but the SQLite default (and dev/docker-compose.yml,
# which mounts a named volume there) writes a file - and Docker initialises an
# empty named volume from the image's ownership at that path. Without this the
# volume would arrive root-owned and every write would fail as `appuser`.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

EXPOSE 8000

# `/healthz` is unauthenticated and touches no dependency, so it answers as long
# as the process is up. `start-period` covers create_db_and_tables() on boot,
# which waits on Postgres. Python rather than curl: this image has no curl, and
# adding one for a healthcheck is a package to patch for no reason.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
