#!/bin/sh
set -eu

APP_DIR="${APP_DIR:-/volume1/docker/ytb-dl}"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "Docker Compose was not found. Install Synology Container Manager first." >&2
  exit 1
fi

mkdir -p "$APP_DIR/config" "$APP_DIR/downloads"
cp "$SCRIPT_DIR/docker-compose.yml" "$COMPOSE_FILE"

cd "$APP_DIR"
$COMPOSE pull
$COMPOSE up -d
$COMPOSE ps

echo "ytb-dl is starting. Open http://<NAS-IP>:9832"
