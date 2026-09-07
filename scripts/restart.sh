#!/usr/bin/env bash
# Restart Relay via systemd when available; otherwise fall back to a pid file.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

restart_unit() {
  local unit="$1"
  if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files --no-legend --type=service 2>/dev/null | grep -q "^${unit}"; then
    if [ "$(id -u)" -eq 0 ]; then
      systemctl restart "$unit"
      systemctl --no-pager --full status "$unit" || true
    else
      sudo systemctl restart "$unit"
      sudo systemctl --no-pager --full status "$unit" || true
    fi
    echo "Restarted ${unit}"
    return 0
  fi
  return 1
}

if restart_unit telegram-relay.service || restart_unit relay.service; then
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
