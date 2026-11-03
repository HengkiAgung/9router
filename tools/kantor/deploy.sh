#!/usr/bin/env bash
# Install or update Kantor as a systemd service on this server. Run from the repo as a sudo user:
#   bash tools/kantor/deploy.sh
# Settings come from the repo .env (KANTOR_*, NINEROUTER_API_KEY); re-run after changing them or the code.
#
# Kantor runs as its own user `kantor` (no sudo, cannot see /home/ubuntu), because its agents run
# shell commands. Its secrets go to /etc/kantor/kantor.env (root only, read by systemd), and only
# the hash of the login password leaves .env.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
ENV_FILE="$REPO/.env"
[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE"; exit 1; }
get() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
[ -n "$(get NINEROUTER_API_KEY)" ] || { echo "Set NINEROUTER_API_KEY in .env"; exit 1; }

if [ -z "$(get KANTOR_PASSWORD)" ]; then
  printf '\n# Kantor login password (deploy.sh puts only its hash on the service)\nKANTOR_PASSWORD=%s\n' \
    "$(openssl rand -base64 18 | tr -d '/+=')" >> "$ENV_FILE"
  echo "Generated KANTOR_PASSWORD in .env"
fi
DOMAIN=$(get KANTOR_DOMAIN)
PORT=$(get KANTOR_PORT); PORT=${PORT:-4610}
MEM_MAX=$(get KANTOR_MEMORY_MAX_MB); MEM_HIGH=$(get KANTOR_MEMORY_HIGH_MB)
BASE_URL=$(get KANTOR_BASE_URL); BASE_URL=${BASE_URL:-http://127.0.0.1:20128/v1}

echo "== user kantor"
id kantor >/dev/null 2>&1 || sudo useradd --create-home --shell /bin/bash kantor
cd /  # kantor can't enter the repo's folder; its commands below must not start there
sudo -u kantor git config --global user.name "Kantor"
sudo -u kantor git config --global user.email "kantor@$(hostname)"
sudo -u kantor git config --global init.defaultBranch main
sudo -u kantor mkdir -p /home/kantor/projects
sudo -u kantor test -f /home/kantor/.ssh/id_ed25519 ||
  sudo -u kantor ssh-keygen -q -t ed25519 -N '' -C "kantor@$(hostname)" -f /home/kantor/.ssh/id_ed25519
sudo -u kantor sh -c 'ssh-keygen -F github.com >/dev/null 2>&1 || ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null'

echo "== code -> /opt/kantor"
sudo install -d -m 755 /opt/kantor/tools/kantor
sudo install -m 644 "$REPO/tools/orchestrator.py" /opt/kantor/tools/
sudo install -m 644 "$REPO/tools/kantor/engine.py" "$REPO/tools/kantor/server.py" "$REPO/tools/kantor/ui.html" \
  /opt/kantor/tools/kantor/

echo "== config -> /etc/kantor/kantor.env"
HASH=$(get KANTOR_PASSWORD | python3 -I /opt/kantor/tools/kantor/server.py hash-password)
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
{
  echo "NINEROUTER_API_KEY=$(get NINEROUTER_API_KEY)"
  echo "KANTOR_BASE_URL=$BASE_URL"
  echo "KANTOR_PASSWORD_HASH=$HASH"
  echo "KANTOR_PORT=$PORT"
  echo "KANTOR_DATA_DIR=/var/lib/kantor"
  echo "KANTOR_PROJECTS_DIR=/home/kantor/projects"
  grep -E '^KANTOR_(MAX_|MIN_|AUTO_|MODELS=|DEFAULT_MODEL=|FALLBACK_MODELS=|COOKIE_SECURE=)' "$ENV_FILE" || true
} > "$TMP"
sudo install -d -m 700 /etc/kantor
sudo install -m 600 -o root -g root "$TMP" /etc/kantor/kantor.env

echo "== systemd service"
sudo tee /etc/systemd/system/kantor.service >/dev/null <<EOF
[Unit]
Description=Kantor multi-agent office
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=kantor
Group=kantor
EnvironmentFile=/etc/kantor/kantor.env
ExecStart=/usr/bin/python3 -I /opt/kantor/tools/kantor/server.py
WorkingDirectory=/home/kantor
StateDirectory=kantor
StateDirectoryMode=0700
Restart=on-failure
RestartSec=3
# RAM limits from .env (KANTOR_MEMORY_*); they cover the commands agents run too.
MemoryHigh=${MEM_HIGH:-400}M
MemoryMax=${MEM_MAX:-512}M
MemorySwapMax=512M
# Agents run shell commands: no privilege gain, read-only system, other homes hidden.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=tmpfs
BindPaths=/home/kantor
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable kantor >/dev/null 2>&1
sudo systemctl restart kantor
for _ in $(seq 20); do curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null && echo "Kantor is up on 127.0.0.1:$PORT" ||
  { sudo journalctl -u kantor -n 30 --no-pager; exit 1; }

if [ -n "$DOMAIN" ]; then
  SITE=/etc/nginx/sites-available/$DOMAIN
  if [ ! -f "$SITE" ]; then
    echo "== nginx site $DOMAIN"
    sudo tee "$SITE" >/dev/null <<EOF
# Kantor — multi-agent office, systemd service kantor on 127.0.0.1:$PORT (tools/kantor/deploy.sh)
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    client_max_body_size 2m;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Connection "";
        # Live event stream (SSE): no buffering, long-lived.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }

    access_log /var/log/nginx/$DOMAIN.access.log;
    error_log  /var/log/nginx/$DOMAIN.error.log;
}
EOF
    sudo ln -sf "$SITE" "/etc/nginx/sites-enabled/$DOMAIN"
    if ! sudo nginx -t 2>/dev/null; then
      sudo rm -f "/etc/nginx/sites-enabled/$DOMAIN"
      echo "nginx config test failed; site disabled"; exit 1
    fi
    sudo systemctl reload nginx
    sudo certbot --nginx -d "$DOMAIN" --non-interactive --redirect --keep-until-expiring ||
      echo "certbot failed: check that $DOMAIN points at this server, then run: sudo certbot --nginx -d $DOMAIN"
  fi
fi

echo
echo "Kantor's SSH key (add it to GitHub to clone private repos, or as a deploy key per repo):"
sudo cat /home/kantor/.ssh/id_ed25519.pub
