#!/usr/bin/env bash
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/cloud-lab-platform}
PUBLIC_HOST=${PUBLIC_HOST:-$(curl -fsS --max-time 2 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || hostname -f)}

if ! command -v docker >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y docker git unzip rsync
    systemctl enable --now docker
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker git unzip rsync
    systemctl enable --now docker
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl unzip rsync
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    echo "Unsupported Linux distribution: install Docker Compose manually first." >&2
    exit 1
  fi
elif ! docker compose version >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y docker-compose-plugin || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y docker-compose-plugin || true
  fi
fi

if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64) COMPOSE_ARCH="x86_64" ;;
    aarch64|arm64) COMPOSE_ARCH="aarch64" ;;
    *) echo "Unsupported architecture for Docker Compose: $ARCH" >&2; exit 1 ;;
  esac
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.1/docker-compose-linux-${COMPOSE_ARCH}" -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.env' ./ "$APP_DIR"/
cd "$APP_DIR"

mkdir -p infra/nginx/certs
if [ -n "${PUBLIC_HOST:-}" ] && [ -f "/etc/letsencrypt/live/${PUBLIC_HOST}/fullchain.pem" ] && [ -f "/etc/letsencrypt/live/${PUBLIC_HOST}/privkey.pem" ]; then
  cp "/etc/letsencrypt/live/${PUBLIC_HOST}/fullchain.pem" infra/nginx/certs/fullchain.pem
  cp "/etc/letsencrypt/live/${PUBLIC_HOST}/privkey.pem" infra/nginx/certs/privkey.pem
  chmod 600 infra/nginx/certs/privkey.pem
elif [ -f /etc/letsencrypt/live/vishal4u.shop/fullchain.pem ] && [ -f /etc/letsencrypt/live/vishal4u.shop/privkey.pem ]; then
  cp /etc/letsencrypt/live/vishal4u.shop/fullchain.pem infra/nginx/certs/fullchain.pem
  cp /etc/letsencrypt/live/vishal4u.shop/privkey.pem infra/nginx/certs/privkey.pem
  chmod 600 infra/nginx/certs/privkey.pem
elif [ ! -f infra/nginx/certs/fullchain.pem ] || [ ! -f infra/nginx/certs/privkey.pem ]; then
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

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created $APP_DIR/.env. Edit it with AWS, domain, and password values before launching production labs."
fi

COMPOSE_FILES=(-f docker-compose.yml)
if [ -f docker-compose.domain.yml ]; then
  COMPOSE_FILES+=(-f docker-compose.domain.yml)
fi

docker compose "${COMPOSE_FILES[@]}" build
docker compose "${COMPOSE_FILES[@]}" up -d --remove-orphans --pull never
docker compose "${COMPOSE_FILES[@]}" restart nginx
docker compose "${COMPOSE_FILES[@]}" ps
