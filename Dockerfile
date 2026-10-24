# website-watcher web UI + scheduled runs. Built on the Unraid server by deploy.ps1.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Australia/Melbourne

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY watcher ./watcher
COPY profiles ./profiles

# /app/data holds watcher.db (mount it); /app/.env holds the Messenger keys (mount it read-only).
VOLUME /app/data
EXPOSE 8000

# Unraid convention: run as nobody:users so appdata files stay editable from shares.
USER 99:100

HEALTHCHECK --interval=1m --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/run', timeout=4)"

CMD ["python", "-m", "watcher", "web"]
