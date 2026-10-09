#!/usr/bin/env bash
# Lokaldan serverga chiqarish.
#
#   bash deploy.sh my-vps                       # ~/.ssh/config dagi alias
#   DEPLOY_HOST=my-vps DEPLOY_DIR=/srv/cnote bash deploy.sh
#
# Serverda .env va data/ TEGILMAYDI: arxivga ular kirmaydi va ochilganda
# ustidan yozilmaydi. Birinchi marta .env ni qo'lda yaratish kerak.

set -euo pipefail

HOST="${1:-${DEPLOY_HOST:-}}"
DIR="${DEPLOY_DIR:-/opt/claude-note-server}"
if [ -z "$HOST" ]; then
  echo "SSH host kerak:  bash deploy.sh <ssh-host>   (yoki DEPLOY_HOST=...)" >&2
  exit 2
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
ARCHIVE="$(mktemp -t cnote-XXXX).tar.gz"
trap 'rm -f "$ARCHIVE"' EXIT

echo "==> arxiv yig'ilmoqda"
tar czf "$ARCHIVE" -C "$HERE" \
  --exclude='data' --exclude='data-dev' --exclude='__pycache__' \
  --exclude='.env' --exclude='*.pyc' --exclude='test_server.py' \
  --exclude='dev_run.py' \
  app.py db.py alerts.py users.py requirements.txt Dockerfile docker-compose.yml \
  .env.example templates static nginx.conf.example

echo "==> $HOST ga yuborilmoqda"
ssh "$HOST" "mkdir -p $DIR"
scp -q "$ARCHIVE" "$HOST:$DIR/deploy.tar.gz"

echo "==> serverda ochilmoqda va qayta qurilmoqda"
ssh "$HOST" bash -s <<EOF
set -euo pipefail
cd "$DIR"
tar xzf deploy.tar.gz && rm -f deploy.tar.gz

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "!!! .env YARATILDI, LEKIN TO'LDIRILMAGAN: $DIR/.env"
  echo "!!! INGEST_TOKENS va DASHBOARD_PASSWORD ni yozing, keyin:"
  echo "!!!   cd $DIR && docker compose up -d --build"
  exit 0
fi

# Bind-mount host papkasi bilan keladi va root ga tegishli bo'ladi, konteyner
# esa cnote (uid 10001) ostida ishlaydi - egalikni oldindan to'g'rilaymiz,
# aks holda "unable to open database file" bo'ladi.
mkdir -p data
chown -R 10001:10001 data

docker compose up -d --build
sleep 6
docker compose ps
echo
echo "--- health ---"
PORT=\$(grep -E '^HOST_PORT=' .env | cut -d= -f2 || true)
curl -fsS "http://127.0.0.1:\${PORT:-8095}/health" && echo
EOF

echo "==> tayyor"
