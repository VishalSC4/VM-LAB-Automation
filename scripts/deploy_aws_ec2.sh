#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/cloud-lab-platform}
PUBLIC_HOST=${PUBLIC_HOST:-$(curl -fsS --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || hostname -f)}

sudo mkdir -p "$APP_DIR"
sudo chown "$USER":"$USER" "$APP_DIR"
rsync -a --delete --exclude '.env' ./ "$APP_DIR"/
cd "$APP_DIR"

mkdir -p infra/nginx/certs
if [ ! -f infra/nginx/certs/fullchain.pem ] || [ ! -f infra/nginx/certs/privkey.pem ]; then
  if [[ "$PUBLIC_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN="IP:${PUBLIC_HOST}"
  else
    SAN="DNS:${PUBLIC_HOST}"
  fi
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 \
    -keyout infra/nginx/certs/privkey.pem \
    -out infra/nginx/certs/fullchain.pem \
    -subj "/CN=${PUBLIC_HOST}" \
    -addext "subjectAltName=${SAN}"
  chmod 600 infra/nginx/certs/privkey.pem
fi

cp -n .env.example .env || true

COMPOSE_FILES=(-f docker-compose.yml)
if [ -f docker-compose.domain.yml ]; then
  COMPOSE_FILES+=(-f docker-compose.domain.yml)
fi

docker compose "${COMPOSE_FILES[@]}" build
docker compose "${COMPOSE_FILES[@]}" up -d --remove-orphans --pull never
docker compose "${COMPOSE_FILES[@]}" ps
