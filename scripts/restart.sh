#!/usr/bin/env bash
# Fallback restart when systemd unit is not installed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files | grep -q '^relay.service'; then
  sudo systemctl restart relay
  exit 0
fi

mkdir -p "$ROOT/run"
if [ -f "$ROOT/run/relay.pid" ] && kill -0 "$(cat "$ROOT/run/relay.pid")" 2>/dev/null; then
  kill "$(cat "$ROOT/run/relay.pid")" || true
  sleep 1
fi

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
elif [ -f venv/bin/activate ]; then
  # shellcheck disable=SC1091
  . venv/bin/activate
fi

nohup python main.py >"$ROOT/run/relay.log" 2>&1 &
echo $! >"$ROOT/run/relay.pid"
echo "Relay restarted (pid $(cat "$ROOT/run/relay.pid"))"
