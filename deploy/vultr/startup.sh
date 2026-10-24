#!/bin/bash
# Vultr boot script: install Docker, fetch the app, start app + Caddy (automatic HTTPS).
# Secrets arrive as this instance's user_data (an env file), not in this account-wide script.
set -euo pipefail
exec >>/var/log/foresight-setup.log 2>&1
[ -f /opt/foresight/.done ] && { cd /opt/foresight/app/deploy/vultr && docker compose --env-file /opt/foresight/.env up -d; exit 0; }

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git docker.io docker-compose-v2
systemctl enable --now docker
if command -v ufw >/dev/null; then ufw allow 80/tcp; ufw allow 443/tcp; fi

install -d -m 700 /opt/foresight
curl -fsS http://169.254.169.254/latest/user-data -o /opt/foresight/.env || curl -fsS http://169.254.169.254/v1/user-data -o /opt/foresight/.env
chmod 600 /opt/foresight/.env
if ! grep -q '^DOMAIN=' /opt/foresight/.env; then
  IP=$(curl -fsS http://169.254.169.254/latest/meta-data/public-ipv4 || hostname -I | awk '{print $1}')
  echo "DOMAIN=${IP//./-}.sslip.io" >> /opt/foresight/.env
fi

git clone --depth 1 --branch "__REF__" "__REPO__" /opt/foresight/app
cd /opt/foresight/app/deploy/vultr
docker compose --env-file /opt/foresight/.env up -d --build
touch /opt/foresight/.done
