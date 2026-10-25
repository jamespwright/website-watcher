# website-watcher web UI + scheduled runs. Built on the Unraid server by deploy.ps1.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Australia/Melbourne \
    HOME=/tmp

# Google Chrome and Xvfb for the Tesla source: Akamai blocks scripts and headless Chrome, so it
# drives a normal Chrome on a virtual display (see watcher/sources/tesla.py).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata wget ca-certificates xvfb fonts-liberation \
 && wget -qO /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm /tmp/chrome.deb \
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
