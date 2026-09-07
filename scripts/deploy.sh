#!/usr/bin/env bash
# Pull latest main, install deps, restart Relay.
# Used by GitHub Actions (SSH) and safe to run manually on the VPS:
#   bash scripts/deploy.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BRANCH="${RELAY_DEPLOY_BRANCH:-main}"
OWNER="$(stat -c '%U' "$ROOT")"

echo "==> Deploying Relay in $ROOT (branch: $BRANCH)"

if [ ! -d .git ]; then
  echo "ERROR: $ROOT is not a git repository."
  exit 1
fi

# Root deploying into a non-root-owned tree (common on this VPS).
git config --global --add safe.directory "$ROOT" 2>/dev/null || true

echo "==> git fetch / pull"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ]; then
  chown -R "${OWNER}:${OWNER}" "$ROOT/.git" || true
fi

echo "==> Python dependencies"
if [ -d .venv ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
elif [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  . venv/bin/activate
else
  echo "WARN: no .venv found — using system Python"
fi

python -m pip install --upgrade pip
pip install -r requirements.txt

echo "==> Restart"
bash "$ROOT/scripts/restart.sh"

echo "==> Deploy complete ($(git rev-parse --short HEAD))"
