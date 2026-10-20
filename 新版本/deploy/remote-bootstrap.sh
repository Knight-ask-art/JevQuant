#!/usr/bin/env bash
# Bootstrap the remote testnet simulation host.
#
# Run as the `ubuntu` user with passwordless sudo. Expects the deployment
# archive at /tmp/jevquant-deploy.tar.gz:
#
#   scp data/jevquant-deploy.tar.gz instance-20260806:/tmp/
#   ssh instance-20260806 'bash -s' < deploy/remote-bootstrap.sh
#
# Re-running is safe: Docker is only installed once, .env is only created once.
set -euo pipefail

TARBALL="${TARBALL:-/tmp/jevquant-deploy.tar.gz}"
TARGET="${TARGET:-$HOME/jevquant}"

if ! command -v docker >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl gnupg
  sudo install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  fi
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
fi

mkdir -p "$(dirname "$TARGET")"
tar -xzf "$TARBALL" -C "$(dirname "$TARGET")"

APP_DIR="$TARGET"
cd "$APP_DIR"

if [ ! -f .env ]; then
  cp deploy/server.env.example .env
  APP_DIR="$APP_DIR" python3 - <<'PY'
import os
import re
import secrets
from pathlib import Path

path = Path(os.environ["APP_DIR"]) / ".env"
text = path.read_text()
for key in ("POSTGRES_PASSWORD", "JEV_ADMIN_API_KEY", "JEV_INTERNAL_EXECUTION_KEY"):
    text = re.sub(
        rf"^{key}=__CHANGE_ME_LONG_RANDOM__$",
        f"{key}={secrets.token_hex(32)}",
        text,
        flags=re.M,
    )
path.write_text(text)
PY
  chmod 600 .env
fi

sudo docker compose pull db redis
sudo docker compose up -d db redis
# Build sequentially: this host has ~1 GB RAM and a 2 GB swap file.
sudo docker compose build api
sudo docker compose build web
# A container that was recreated over a still-running process (a restart that
# never reaped it) is left "Created" and blocks the next `up`. Removing it is
# safe: it is not serving anything.
sudo docker compose rm -f --stop api >/dev/null 2>&1 || true
sudo docker compose up -d
sudo docker compose ps

cat <<EOF

Env file: $APP_DIR/.env
Admin key: sudo grep '^JEV_ADMIN_API_KEY=' "$APP_DIR/.env"

Next: fill the testnet credentials (and JEV_PAPER_STARTING_EQUITY) in .env:
  nano "$APP_DIR/.env"
  cd "$APP_DIR" && sudo docker compose up -d --force-recreate api

UI over an SSH tunnel:
  ssh -L 8080:127.0.0.1:8080 instance-20260806
  open http://127.0.0.1:8080 and paste the admin key in Audit & Settings
EOF
