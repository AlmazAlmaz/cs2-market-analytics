#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$PROJECT_DIR/.run"
PID_FILE="$RUN_DIR/start_all.pid"

read_pid() {
  [[ -f "$PID_FILE" ]] && tr -d '[:space:]' < "$PID_FILE" || true
}

pid="$(read_pid)"
if [[ -z "$pid" ]]; then
  echo "CS2 services are already stopped (no $PID_FILE)."
  exit 0
fi
if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  echo "Refusing to use malformed supervisor PID file: $PID_FILE" >&2
  exit 1
fi
if ! kill -0 "$pid" >/dev/null 2>&1; then
  rm -f "$PID_FILE"
  echo "CS2 services are already stopped (removed stale PID file)."
  exit 0
fi

command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
case "$command_line" in
  *start_all.sh*) ;;
  *)
    echo "Refusing to signal PID $pid: it is not the start_all.sh supervisor." >&2
    exit 1
    ;;
esac

echo "Stopping CS2 supervisor PID $pid..."
kill -TERM "$pid"
for _attempt in $(seq 1 80); do
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$PID_FILE"
    echo "All services started by start_all.sh have stopped."
    exit 0
  fi
  sleep 0.25
done

echo "Supervisor PID $pid did not stop within 20 seconds." >&2
echo "Check $PROJECT_DIR/logs and run this command again." >&2
exit 1
