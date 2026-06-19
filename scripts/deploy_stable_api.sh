#!/usr/bin/env bash
set -euo pipefail

HOST="${1:-170.106.75.116}"
TARGET="${2:-/opt/weiq-scraper-stable-api}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${TARGET}/backups/release_${STAMP}"

cd "$ROOT_DIR"
PYTHON="${ROOT_DIR}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Missing stable-api virtual environment: $PYTHON" >&2
  exit 1
fi

"$PYTHON" -m pytest tests -q
ssh "root@${HOST}" "mkdir -p '$BACKUP_DIR'; \
  tar -C '$TARGET' \
    --exclude='./.git' --exclude='./.venv' --exclude='./backups' \
    --exclude='./runtime' --exclude='./runtime_local' \
    --exclude='./browser_profile' --exclude='./state.json' \
    --exclude='./crawl_state.json' --exclude='./*.db' \
    -czf '$BACKUP_DIR/source.tgz' .; \
  cp -a '$TARGET/weiq_local.db' '$BACKUP_DIR/weiq_local.db' 2>/dev/null || true; \
  cp -a '$TARGET/state.json' '$BACKUP_DIR/state.json' 2>/dev/null || true; \
  tar -C '$TARGET' -czf '$BACKUP_DIR/browser_profile.tgz' browser_profile 2>/dev/null || true"

rollback() {
  echo "Deployment failed; restoring previous stable-api source" >&2
  ssh "root@${HOST}" "tar -C '$TARGET' -xzf '$BACKUP_DIR/source.tgz' && systemctl restart weiq-scraper" || true
}
trap rollback ERR

rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.pytest_cache/' \
  --exclude '__pycache__/' \
  --exclude 'backups/' \
  --exclude 'runtime/' \
  --exclude 'runtime_local/' \
  --exclude 'browser_profile/' \
  --exclude 'state.json' \
  --exclude 'crawl_state.json' \
  --exclude '*.db' \
  "$ROOT_DIR/" "root@${HOST}:${TARGET}/"

ssh "root@${HOST}" "cd /tmp && '$TARGET/.venv/bin/python' -m py_compile '$TARGET/cloud_api.py' '$TARGET/scraper_runtime.py' && systemctl restart weiq-scraper"
ssh "root@${HOST}" "curl -fsS --max-time 10 http://127.0.0.1:8080/health"

trap - ERR
echo
echo "Stable API deployed. Backup: $BACKUP_DIR"
