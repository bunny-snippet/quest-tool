#!/usr/bin/env bash
set -euo pipefail

cd /opt/quest-tool
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate --noinput
if grep -Eqi '^PRESCREENER_VAULT_ENABLED=(true|1|yes|on)$' .env; then
  .venv/bin/python manage.py migrate --database=prescreener_vault --noinput
fi
.venv/bin/python manage.py collectstatic --noinput

public_static_dir="${PUBLIC_STATIC_DIR:-$HOME/htdocs/api.exchange-ip.com/static}"
if test -d "$(dirname "$public_static_dir")"; then
  mkdir -p "$public_static_dir"
  cp -a staticfiles/. "$public_static_dir/"
  chmod -R u=rwX,g=rX,o= "$public_static_dir"
fi
.venv/bin/python manage.py check --deploy
sudo systemctl restart quest-tool-web quest-tool-worker quest-tool-beat
sudo systemctl --no-pager --full status quest-tool-web quest-tool-worker quest-tool-beat
