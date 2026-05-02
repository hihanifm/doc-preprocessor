#!/usr/bin/env bash
# Restart Docs Garage (Flask) under nohup: stop old server, start again.
# Usage: ./restart-app-nohup.sh
# Env:   HOST (default 127.0.0.1), PORT (default 5000),
#        LOG (default nohup-app.log), PIDFILE (default .app-nohup.pid)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"
LOG="${LOG:-nohup-app.log}"
PIDFILE="${PIDFILE:-.app-nohup.pid}"

if [[ ! -f ".venv/bin/activate" ]]; then
  echo "Missing .venv — run ./start.sh once first." >&2
  exit 1
fi

stop_pid() {
  local pid="$1"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  echo "Stopping PID $pid..."
  kill "$pid" 2>/dev/null || true
  local i
  for i in $(seq 1 25); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.2
  done
  kill -9 "$pid" 2>/dev/null || true
}

if [[ -f "$PIDFILE" ]]; then
  stop_pid "$(cat "$PIDFILE" 2>/dev/null || true)"
  rm -f "$PIDFILE"
fi

if command -v lsof >/dev/null 2>&1; then
  for p in $(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true); do
    echo "Stopping listener on port $PORT (PID $p)..."
    stop_pid "$p"
  done
fi

source ".venv/bin/activate"

echo "Starting (nohup) — http://${HOST}:${PORT}/"
echo "Log: $ROOT/$LOG"

nohup python app.py --host "$HOST" --port "$PORT" >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"

echo "PID $(cat "$PIDFILE")  |  tail: tail -f $LOG"
