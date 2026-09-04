#!/usr/bin/env bash
set -Eeuo pipefail

domain="${1:?domain required}"
archive="/tmp/arhibot-main.tar.gz"
app_dir="$HOME/arhibot"
env_backup="/tmp/arhibot.env.backup"

if [[ -f "$app_dir/backend/.env" ]]; then
  cp "$app_dir/backend/.env" "$env_backup"
fi
rm -rf "$app_dir"
mkdir -p "$app_dir"
tar -xzf "$archive" -C "$app_dir" --strip-components=1
rm -f "$archive"
if [[ -f "$env_backup" ]]; then
  cp "$env_backup" "$app_dir/backend/.env"
fi

cd "$app_dir/backend"
set_key() {
  local key="$1" value="$2" file=.env
  if grep -qE "^${key}=" "$file" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

if [[ ! -f .env ]]; then
  touch .env
  chmod 600 .env
  set_key JWT_SECRET "$(openssl rand -hex 32)"
  set_key REFRESH_TOKEN_SECRET "$(openssl rand -hex 32)"
fi

set_key APP_ENV production
set_key APP_NAME 'AI Architecture Platform API'
set_key APP_VERSION 0.3.0
set_key API_V1_PREFIX /api/v1
set_key LOG_LEVEL INFO
set_key DATABASE_URL 'postgresql+asyncpg://app:app@postgres:5432/app'
set_key REDIS_URL 'redis://redis:6379/0'
set_key CORS_ORIGINS "https://${domain}"
set_key JWT_ALGORITHM HS256
set_key JWT_ISSUER ai-architecture-platform
set_key JWT_AUDIENCE ai-architecture-api
set_key ACCESS_TOKEN_TTL_SECONDS 900
set_key REFRESH_TOKEN_TTL_SECONDS 2592000
set_key TELEGRAM_WEBAPP_URL "https://${domain}"
set_key TELEGRAM_INIT_DATA_TTL_SECONDS 3600
set_key MEDIA_ROOT /data/media
set_key MEDIA_PUBLIC_BASE_URL "https://${domain}"
set_key MAX_IMAGE_SIZE_BYTES 20971520
set_key MAX_IMAGE_PIXELS 80000000
set_key FRONTEND_APP_NAME ArchiAI
set_key VITE_DEMO_GENERATION true
chmod 600 .env

docker compose up -d postgres redis
for _ in $(seq 1 30); do
  docker compose exec -T postgres pg_isready -U app -d app >/dev/null 2>&1 && break
  sleep 2
done
docker compose exec -T postgres pg_isready -U app -d app >/dev/null

docker compose run --rm api alembic upgrade head
docker compose up -d --build api frontend nginx
for _ in $(seq 1 40); do
  curl -fsS --max-time 3 http://127.0.0.1:18080/health/live >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://127.0.0.1:18080/health/live >/dev/null

docker network connect arhibot_default artflow-nginx-1 2>/dev/null || true
nginx_conf=$(docker inspect artflow-nginx-1 --format '{{range .Mounts}}{{if eq .Destination "/etc/nginx/conf.d/default.conf"}}{{.Source}}{{end}}{{end}}')
[[ -n "$nginx_conf" && -f "$nginx_conf" ]]
cp "$nginx_conf" "$nginx_conf.bak.$(date +%Y%m%d%H%M%S)"

strip_block() {
  awk '
    /^# BEGIN ARHIBOT MANAGED$/ {skip=1; next}
    /^# END ARHIBOT MANAGED$/ {skip=0; next}
    !skip {print}
  ' "$nginx_conf" > "$nginx_conf.tmp"
  mv "$nginx_conf.tmp" "$nginx_conf"
}

strip_block
cat >> "$nginx_conf" <<HTTP_BLOCK

# BEGIN ARHIBOT MANAGED
server {
    listen 80;
    server_name ${domain};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
# END ARHIBOT MANAGED
HTTP_BLOCK

docker exec artflow-nginx-1 nginx -t
docker exec artflow-nginx-1 nginx -s reload

certbot certonly --webroot -w /var/www/certbot -d "$domain" \
  --non-interactive --agree-tos --register-unsafely-without-email --keep-until-expiring

strip_block
cat >> "$nginx_conf" <<HTTPS_BLOCK

# BEGIN ARHIBOT MANAGED
server {
    listen 80;
    server_name ${domain};
    location /.well-known/acme-challenge/ { root /var/www/certbot; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl;
    server_name ${domain};
    ssl_certificate /etc/letsencrypt/live/${domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${domain}/privkey.pem;
    client_max_body_size 21m;
    location / {
        proxy_pass http://arhibot-nginx-1:80;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
# END ARHIBOT MANAGED
HTTPS_BLOCK

docker exec artflow-nginx-1 nginx -t
docker exec artflow-nginx-1 nginx -s reload
curl -fsS --max-time 10 "https://${domain}/health/live" >/dev/null
curl -fsS --max-time 10 -o /dev/null "https://${domain}/"
echo "PUBLIC_URL=https://${domain}"
docker compose ps
