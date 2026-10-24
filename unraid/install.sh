#!/bin/bash
# Runs on the Unraid server (deploy.ps1 calls it after uploading the code to $APPDATA/src):
# builds the image and (re)creates the container with the same settings as website-watcher.xml.
set -euo pipefail

NAME=website-watcher
APPDATA=/mnt/user/appdata/$NAME
SRC=$APPDATA/src
PORT=${PORT:-8000}
SCHEDULE=${WATCHER_SCHEDULE:-06:00,12:00,18:00}
TEMPLATES=/boot/config/plugins/dockerMan/templates-user
AUTOSTART=/var/lib/docker/unraid-autostart

mkdir -p "$APPDATA/data"
[ -f "$APPDATA/.env" ] || touch "$APPDATA/.env"
chown -R 99:100 "$APPDATA/data" "$APPDATA/.env"

echo "== Building image"
docker build -t "$NAME:latest" "$SRC"

echo "== Recreating container"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --user 99:100 \
  -p "$PORT:8000" \
  -v "$APPDATA/data:/app/data" \
  -v "$APPDATA/.env:/app/.env:ro" \
  -e WATCHER_SCHEDULE="$SCHEDULE" \
  -e TZ="${TZ:-Australia/Melbourne}" \
  -l net.unraid.docker.managed=dockerman \
  -l "net.unraid.docker.webui=http://[IP]:[PORT:8000]/" \
  "$NAME:latest" >/dev/null

# Show up (and be editable) in Unraid's Docker tab, and start with the array.
mkdir -p "$TEMPLATES"
cp "$SRC/unraid/$NAME.xml" "$TEMPLATES/my-$NAME.xml"
grep -qx "$NAME" "$AUTOSTART" 2>/dev/null || echo "$NAME" >> "$AUTOSTART"

docker image prune -f >/dev/null  # drop the previous build's untagged image

sleep 3
docker ps --filter "name=^$NAME\$" --format '== {{.Names}}: {{.Status}}'
docker logs --tail 20 "$NAME"
