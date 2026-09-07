#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
PUBLIC_TUNNEL="${PUBLIC_TUNNEL:-0}"
OPEN_BROWSER="${OPEN_BROWSER:-1}"
KEEP_AWAKE="${KEEP_AWAKE:-1}"
START_PARSER="${START_PARSER:-1}"
START_SCANNER="${START_SCANNER:-1}"
START_TREND="${START_TREND:-1}"
START_MARKET_HISTORY="${START_MARKET_HISTORY:-auto}"
START_REVERSE_TUNNEL="${START_REVERSE_TUNNEL:-auto}"
START_API="${START_API:-auto}"
START_TELEGRAM="${START_TELEGRAM:-auto}"
PUBLIC_SITE_URL="${PUBLIC_SITE_URL:-}"
# The admin bootstrap URL contains a bearer token. Keep it out of terminal
# captures by default; OPEN_BROWSER can still exchange it for the HttpOnly
# cookie without printing it.
QUIET_DASHBOARD_URL="${QUIET_DASHBOARD_URL:-1}"
LOG_DIR="$PROJECT_DIR/logs"
RUN_DIR="$PROJECT_DIR/.run"
START_ALL_PID_FILE="$RUN_DIR/start_all.pid"
STARTED_SERVICES=""
CORE_SERVICES=""
REVERSE_TUNNEL_ENABLED=0
PUBLIC_SITE_VERIFIED=0
QUICK_TUNNEL_URL=""
QUICK_TUNNEL_VERIFIED=0
STARTUP_LOCK_PID=""
STARTUP_LOCK_READY=""

mkdir -p "$LOG_DIR" "$RUN_DIR"
chmod 700 "$LOG_DIR" "$RUN_DIR" 2>/dev/null || true
cd "$PROJECT_DIR"

release_startup_singleton() {
  if [[ -n "$STARTUP_LOCK_PID" ]] && kill -0 "$STARTUP_LOCK_PID" >/dev/null 2>&1; then
    kill "$STARTUP_LOCK_PID" >/dev/null 2>&1 || true
    wait "$STARTUP_LOCK_PID" 2>/dev/null || true
  fi
  if [[ -n "$STARTUP_LOCK_READY" ]]; then
    rm -f "$STARTUP_LOCK_READY"
  fi
  if [[ -f "$START_ALL_PID_FILE" ]] && [[ "$(tr -d '[:space:]' < "$START_ALL_PID_FILE")" == "$$" ]]; then
    rm -f "$START_ALL_PID_FILE"
  fi
  STARTUP_LOCK_PID=""
}

acquire_startup_singleton() {
  STARTUP_LOCK_READY="$RUN_DIR/model_lifecycle/.start_all.$$.ready"
  rm -f "$STARTUP_LOCK_READY"
  "$PYTHON_BIN" - "$STARTUP_LOCK_READY" "$$" <<'PY' &
import os
import sys
import time
from pathlib import Path

import model_lifecycle

ready = Path(sys.argv[1])
parent_pid = int(sys.argv[2])
try:
    with model_lifecycle.startup_lifetime_lock():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(ready, flags, 0o600)
        try:
            os.write(descriptor, f"{parent_pid}\n".encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        while os.getppid() == parent_pid:
            time.sleep(0.25)
except model_lifecycle.ModelLifecycleError as exc:
    print(f"start_all lock error: {exc}", file=sys.stderr)
    raise SystemExit(73) from exc
PY
  STARTUP_LOCK_PID="$!"
  local attempt helper_status
  for attempt in $(seq 1 100); do
    if [[ -f "$STARTUP_LOCK_READY" ]]; then
      return 0
    fi
    if ! kill -0 "$STARTUP_LOCK_PID" >/dev/null 2>&1; then
      if wait "$STARTUP_LOCK_PID"; then
        helper_status=0
      else
        helper_status="$?"
      fi
      STARTUP_LOCK_PID=""
      echo "start_all singleton was not acquired (helper exit $helper_status)" >&2
      return 1
    fi
    sleep 0.05
  done
  echo "timed out while acquiring the start_all singleton" >&2
  release_startup_singleton
  return 1
}

trap release_startup_singleton EXIT

validate_switch() {
  local name="$1"
  local value="$2"
  case "$value" in
    0|1) ;;
    *)
      echo "$name must be 0 or 1 (got: $value)." >&2
      exit 1
      ;;
  esac
}

validate_auto_switch() {
  local name="$1"
  local value="$2"
  case "$value" in
    0|1|auto) ;;
    *)
      echo "$name must be 0, 1, or auto (got: $value)." >&2
      exit 1
      ;;
  esac
}

validate_switch PUBLIC_TUNNEL "$PUBLIC_TUNNEL"
validate_switch OPEN_BROWSER "$OPEN_BROWSER"
validate_switch KEEP_AWAKE "$KEEP_AWAKE"
validate_switch START_PARSER "$START_PARSER"
validate_switch START_SCANNER "$START_SCANNER"
validate_switch START_TREND "$START_TREND"
validate_switch QUIET_DASHBOARD_URL "$QUIET_DASHBOARD_URL"
validate_auto_switch START_MARKET_HISTORY "$START_MARKET_HISTORY"
validate_auto_switch START_REVERSE_TUNNEL "$START_REVERSE_TUNNEL"
validate_auto_switch START_API "$START_API"
validate_auto_switch START_TELEGRAM "$START_TELEGRAM"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python environment not found: $PYTHON_BIN" >&2
  echo "Run ./setup_mac.sh first." >&2
  exit 1
fi
if ! acquire_startup_singleton; then
  exit 1
fi
printf '%s\n' "$$" > "$START_ALL_PID_FILE"
chmod 600 "$START_ALL_PID_FILE" 2>/dev/null || true
if ! "$PYTHON_BIN" -m pip check >/dev/null; then
  echo "The Python environment has broken dependencies. Run ./setup_mac.sh again." >&2
  exit 1
fi

# A completed dashboard retraining job never replaces a model while the site
# is live.  Apply only quality-gated pending bundles here, before any service
# is started.  Any activation or crash-recovery error is fail-closed: starting
# consumers against a potentially mixed bundle would be unsafe.
if ! "$PYTHON_BIN" model_training_activation.py --activate-pending --offline-confirm; then
  echo "error: pending model activation/recovery failed; services were not started" >&2
  echo "check .run/model_training/pending and .run/model_lifecycle before retrying" >&2
  exit 1
fi

CHECK_CSFLOAT_KEYS=0
if [[ "$START_PARSER" == "1" || "$START_SCANNER" == "1" ]]; then
  CHECK_CSFLOAT_KEYS=1
fi
CS2_CHECK_CSFLOAT_KEYS="$CHECK_CSFLOAT_KEYS" "$PYTHON_BIN" - <<'PY'
import os
import stat

import app_config

required = {
    "database": app_config.path("CS2_DB_PATH", "csfloat_history.db"),
    "skin catalog": app_config.path("CS2_SKINS_PATH", "ALL_SKINS.json"),
    "Almaz model": app_config.PROJECT_DIR / "models" / "csfloat_price_model.joblib",
    "Igor point model": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "price_model_point.cbm",
    "Igor low model": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "price_model_q15.cbm",
    "Igor high model": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "price_model_q85.cbm",
    "Igor metadata": app_config.PROJECT_DIR / "IGORMODEL" / "artifacts" / "feature_metadata.pkl",
    "Nova model": app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts" / "nova_price_model.pt",
    "Nova metadata": app_config.PROJECT_DIR / "NEURALMODEL" / "artifacts" / "nova_metadata.json",
}
missing = [f"{label}: {path}" for label, path in required.items() if not path.is_file()]
if missing:
    raise SystemExit("Missing required project files:\n- " + "\n- ".join(missing))

if app_config.CONFIG_PATH.exists():
    mode = stat.S_IMODE(app_config.CONFIG_PATH.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"{app_config.CONFIG_PATH} must be private; run chmod 600 on it")

if os.environ.get("CS2_CHECK_CSFLOAT_KEYS") == "1":
    from csfloat_listing_fetcher import load_key_records

    if not load_key_records(strict=True):
        raise SystemExit("No usable CSFloat API routes were found")

print("preflight: database, skins, models and CSFloat routes are ready")
PY

if [[ "$START_TREND" == "1" ]]; then
  if ! "$PYTHON_BIN" -c 'import TRENDMODEL, trend_worker' >/dev/null; then
    echo "Trend worker dependencies could not be imported." >&2
    exit 1
  fi
fi

DASHBOARD_HOST="$($PYTHON_BIN -c 'import app_config; print(app_config.get("DASHBOARD_HOST", "127.0.0.1"))')"
DASHBOARD_PORT="$($PYTHON_BIN -c 'import app_config; print(app_config.get_int("DASHBOARD_PORT", 8010, minimum=1, maximum=65535))')"
HEALTH_HOST="$DASHBOARD_HOST"
if [[ "$HEALTH_HOST" == "0.0.0.0" || "$HEALTH_HOST" == "::" ]]; then
  HEALTH_HOST="127.0.0.1"
fi
HEALTH_URL="http://$HEALTH_HOST:$DASHBOARD_PORT/health"

is_running() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1
}

pid_from_file() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] && tr -d '[:space:]' < "$pid_file" || true
}

rotate_log() {
  local log_file="$1"
  local log_size=0
  if [[ -f "$log_file" ]]; then
    log_size="$(stat -f %z "$log_file" 2>/dev/null || stat -c %s "$log_file")"
  fi
  if (( log_size > 20971520 )); then
    mv -f "$log_file" "$log_file.1"
  fi
}

remember_started_service() {
  local name="$1"
  if [[ " $STARTED_SERVICES " != *" $name "* ]]; then
    STARTED_SERVICES="$STARTED_SERVICES $name"
  fi
}

start_service() {
  local name="$1"
  shift
  local pid_file="$RUN_DIR/$name.pid"
  local log_file="$LOG_DIR/$name.log"
  local old_pid
  old_pid="$(pid_from_file "$pid_file")"

  if is_running "$old_pid"; then
    echo "$name already running: pid $old_pid"
    return 0
  fi
  rm -f "$pid_file"
  rotate_log "$log_file"
  echo "starting $name -> $log_file"
  "$@" >> "$log_file" 2>&1 &
  local pid="$!"
  echo "$pid" > "$pid_file"
  chmod 600 "$pid_file" 2>/dev/null || true
  remember_started_service "$name"
  echo "$name started: pid $pid"
}

stop_started_services() {
  trap - INT TERM EXIT
  if [[ -z "${STARTED_SERVICES// }" ]]; then
    release_startup_singleton
    return
  fi
  echo
  echo "stopping services started by this command..."
  local name pid pid_file
  for name in $STARTED_SERVICES; do
    pid_file="$RUN_DIR/$name.pid"
    pid="$(pid_from_file "$pid_file")"
    if is_running "$pid"; then
      echo "stopping $name: pid $pid"
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
  # Give signal-aware workers time to flush their final heartbeat/status before
  # using the existing last-resort SIGKILL cleanup.
  local attempt all_stopped
  for attempt in $(seq 1 20); do
    all_stopped=1
    for name in $STARTED_SERVICES; do
      pid_file="$RUN_DIR/$name.pid"
      pid="$(pid_from_file "$pid_file")"
      if is_running "$pid"; then
        all_stopped=0
        break
      fi
    done
    if [[ "$all_stopped" == "1" ]]; then
      break
    fi
    sleep 0.25
  done
  for name in $STARTED_SERVICES; do
    pid_file="$RUN_DIR/$name.pid"
    pid="$(pid_from_file "$pid_file")"
    if is_running "$pid"; then
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$pid_file"
  done
  rm -f "$RUN_DIR/public_url.txt"
  release_startup_singleton
}

trap 'exit 130' INT
trap 'exit 143' TERM
trap stop_started_services EXIT

wait_for_dashboard() {
  local dashboard_pid
  dashboard_pid="$(pid_from_file "$RUN_DIR/dashboard.pid")"
  local attempt health_payload
  for attempt in $(seq 1 180); do
    health_payload="$(curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null || true)"
    if printf '%s' "$health_payload" | grep -Eq '"service"[[:space:]]*:[[:space:]]*"cs2-dashboard"'; then
      echo "dashboard is healthy: $HEALTH_URL"
      return 0
    fi
    if [[ -n "$dashboard_pid" ]] && ! is_running "$dashboard_pid"; then
      echo "dashboard exited during startup. Last log lines:" >&2
      tail -n 40 "$LOG_DIR/dashboard.log" >&2 || true
      return 1
    fi
    sleep 1
  done
  echo "dashboard did not become healthy in 180 seconds. Check $LOG_DIR/dashboard.log" >&2
  return 1
}

wait_for_service_stable() {
  local name="$1"
  local seconds="${2:-5}"
  local pid attempt
  pid="$(pid_from_file "$RUN_DIR/$name.pid")"
  for attempt in $(seq 1 "$seconds"); do
    if ! is_running "$pid"; then
      echo "$name exited during startup. Last log lines:" >&2
      tail -n 40 "$LOG_DIR/$name.log" >&2 || true
      return 1
    fi
    sleep 1
  done
  echo "$name is stable"
}

wait_for_api() {
  local api_pid api_payload attempt
  api_pid="$(pid_from_file "$RUN_DIR/api_server.pid")"
  for attempt in $(seq 1 60); do
    api_payload="$(curl -fsS --max-time 3 "$API_HEALTH_URL" 2>/dev/null || true)"
    if printf '%s' "$api_payload" | grep -Eq '"service"[[:space:]]*:[[:space:]]*"cs2-export-api"'; then
      echo "export API is healthy: $API_HEALTH_URL"
      return 0
    fi
    if ! is_running "$api_pid"; then
      echo "api_server exited during startup. Last log lines:" >&2
      tail -n 40 "$LOG_DIR/api_server.log" >&2 || true
      return 1
    fi
    sleep 1
  done
  echo "export API did not become healthy. Check $LOG_DIR/api_server.log" >&2
  return 1
}

wait_for_public_site() {
  local payload attempt
  [[ -n "$PUBLIC_SITE_URL" ]] || return 1
  for attempt in $(seq 1 6); do
    payload="$(curl -fsS --max-time 3 "$PUBLIC_SITE_URL/health" 2>/dev/null || true)"
    if printf '%s' "$payload" | grep -Eq '"service"[[:space:]]*:[[:space:]]*"cs2-dashboard"'; then
      echo "public site is healthy: $PUBLIC_SITE_URL/health"
      return 0
    fi
    sleep 1
  done
  return 1
}

wait_for_quick_tunnel() {
  local pid url attempt
  pid="$(pid_from_file "$RUN_DIR/public_tunnel.pid")"
  url=""
  for attempt in $(seq 1 120); do
    if ! is_running "$pid"; then
      echo "cloudflared exited before the tunnel became reachable." >&2
      tail -n 30 "$LOG_DIR/public_tunnel.log" >&2 || true
      return 1
    fi
    url="$(grep -Eo 'https://[-a-zA-Z0-9]+\.trycloudflare\.com' "$LOG_DIR/public_tunnel.log" | tail -n 1 || true)"
    if [[ -n "$url" ]] && curl -fsS --max-time 5 "$url/health" >/dev/null 2>&1; then
      echo "$url" > "$RUN_DIR/public_url.txt"
      chmod 600 "$RUN_DIR/public_url.txt" 2>/dev/null || true
      echo "temporary public site: $url"
      return 0
    fi
    sleep 1
  done
  echo "quick tunnel was not reachable in 120 seconds." >&2
  return 1
}

reverse_check_output=""
case "$START_REVERSE_TUNNEL" in
  0) ;;
  1)
    reverse_check_output="$($PROJECT_DIR/start_reverse_tunnel.sh --check)"
    printf '%s\n' "$reverse_check_output"
    REVERSE_TUNNEL_ENABLED=1
    ;;
  auto)
    if reverse_check_output="$($PROJECT_DIR/start_reverse_tunnel.sh --check 2>&1)"; then
      printf '%s\n' "$reverse_check_output"
      REVERSE_TUNNEL_ENABLED=1
    else
      echo "reverse tunnel: skipped (hosting.local.env is incomplete; run ./start_reverse_tunnel.sh --check)"
    fi
    ;;
esac

API_STATUS="$($PYTHON_BIN - <<'PY'
import app_config

token = app_config.read_secret("API_TOKEN", app_config.path("API_TOKEN_PATH", "api_token.txt"))
database = app_config.path("API_DB_PATH", "csfloat_sales_history.db")
if not token:
    print("missing_token")
elif not database.is_file():
    print("missing_database")
else:
    print("ready")
PY
)"
case "$API_STATUS" in
  ready) API_STATUS_MESSAGE="ready" ;;
  missing_token) API_STATUS_MESSAGE="API_TOKEN is empty" ;;
  missing_database) API_STATUS_MESSAGE="API_DB_PATH does not exist" ;;
  *) API_STATUS_MESSAGE="$API_STATUS" ;;
esac
API_ENABLED=0
if [[ "$START_API" == "1" && "$API_STATUS" != "ready" ]]; then
  echo "START_API=1, but the export API is not configured ($API_STATUS_MESSAGE)." >&2
  exit 1
elif [[ "$START_API" == "1" || ( "$START_API" == "auto" && "$API_STATUS" == "ready" ) ]]; then
  API_ENABLED=1
elif [[ "$START_API" == "auto" ]]; then
  echo "export API: skipped ($API_STATUS_MESSAGE; it is optional for the site)"
fi

TELEGRAM_STATUS="$($PYTHON_BIN - <<'PY'
import app_config

token = app_config.read_secret("TELEGRAM_BOT_TOKEN", "telegram_bot_token.txt")
allowed = [
    value.strip()
    for value in app_config.get("TELEGRAM_ALLOWED_CHAT_IDS").split(",")
    if value.strip().lstrip("-").isdigit()
]
if not token:
    print("missing_token")
elif not allowed and not app_config.get_bool("TELEGRAM_ALLOW_ALL", False):
    print("missing_allowed_chat_ids")
else:
    print("ready")
PY
)"
case "$TELEGRAM_STATUS" in
  ready) TELEGRAM_STATUS_MESSAGE="ready" ;;
  missing_token) TELEGRAM_STATUS_MESSAGE="TELEGRAM_BOT_TOKEN is empty" ;;
  missing_allowed_chat_ids) TELEGRAM_STATUS_MESSAGE="TELEGRAM_ALLOWED_CHAT_IDS is empty" ;;
  *) TELEGRAM_STATUS_MESSAGE="$TELEGRAM_STATUS" ;;
esac
TELEGRAM_ENABLED=0
if [[ "$START_TELEGRAM" == "1" && "$TELEGRAM_STATUS" != "ready" ]]; then
  echo "START_TELEGRAM=1, but Telegram is not configured ($TELEGRAM_STATUS_MESSAGE)." >&2
  exit 1
elif [[ "$START_TELEGRAM" == "1" || ( "$START_TELEGRAM" == "auto" && "$TELEGRAM_STATUS" == "ready" ) ]]; then
  TELEGRAM_ENABLED=1
elif [[ "$START_TELEGRAM" == "auto" ]]; then
  echo "Telegram bot: skipped ($TELEGRAM_STATUS_MESSAGE; access remains locked)"
fi

MARKET_HISTORY_STATUS="$($PYTHON_BIN - <<'PY'
import app_config

enabled = app_config.get_bool("CS2_MARKET_HISTORY_ENABLED", False)
license_confirmed = app_config.get_bool(
    "CS2_MARKET_HISTORY_LICENSE_CONFIRMED", False
)
api_key = app_config.read_secret("CSPRICEAPI_KEY")
fetch_snapshots = app_config.get_bool("CS2_MARKET_HISTORY_FETCH_SNAPSHOTS", True)
fetch_sales = app_config.get_bool("CS2_MARKET_HISTORY_FETCH_SALES", True)
if not enabled:
    print("disabled")
elif not license_confirmed:
    print("license_unconfirmed")
elif not api_key:
    print("missing_key")
elif not fetch_snapshots and not fetch_sales:
    print("no_fetches")
else:
    print("ready")
PY
)"
case "$MARKET_HISTORY_STATUS" in
  ready) MARKET_HISTORY_STATUS_MESSAGE="ready" ;;
  disabled) MARKET_HISTORY_STATUS_MESSAGE="CS2_MARKET_HISTORY_ENABLED is false" ;;
  license_unconfirmed) MARKET_HISTORY_STATUS_MESSAGE="written provider license is not confirmed" ;;
  missing_key) MARKET_HISTORY_STATUS_MESSAGE="CSPRICEAPI_KEY is empty" ;;
  no_fetches) MARKET_HISTORY_STATUS_MESSAGE="all market history fetches are disabled" ;;
  *) MARKET_HISTORY_STATUS_MESSAGE="$MARKET_HISTORY_STATUS" ;;
esac
MARKET_HISTORY_ENABLED=0
if [[ "$START_MARKET_HISTORY" == "1" && "$MARKET_HISTORY_STATUS" != "ready" ]]; then
  echo "START_MARKET_HISTORY=1, but automatic market history is not configured ($MARKET_HISTORY_STATUS_MESSAGE)." >&2
  exit 1
elif [[ "$START_MARKET_HISTORY" == "1" || ( "$START_MARKET_HISTORY" == "auto" && "$MARKET_HISTORY_STATUS" == "ready" ) ]]; then
  if ! "$PYTHON_BIN" -c 'import market_history, market_history_worker' >/dev/null; then
    echo "Market history worker dependencies could not be imported." >&2
    exit 1
  fi
  MARKET_HISTORY_ENABLED=1
elif [[ "$START_MARKET_HISTORY" == "auto" ]]; then
  echo "market history worker: skipped ($MARKET_HISTORY_STATUS_MESSAGE; no provider request was made)"
fi

if [[ "$KEEP_AWAKE" == "1" ]] && command -v caffeinate >/dev/null 2>&1; then
  start_service keep_awake caffeinate -dimsu -w "$$"
fi

if lsof -nP -iTCP:"$DASHBOARD_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $DASHBOARD_PORT is already in use; checking the existing dashboard"
else
  start_service dashboard "$PYTHON_BIN" -u dashboard_server.py
fi
wait_for_dashboard
CORE_SERVICES="dashboard"

if [[ "$START_TREND" == "1" ]]; then
  start_service trend_worker "$PYTHON_BIN" -u trend_worker.py
  wait_for_service_stable trend_worker 3
  CORE_SERVICES="$CORE_SERVICES trend_worker"
fi

if [[ "$MARKET_HISTORY_ENABLED" == "1" ]]; then
  start_service market_history_worker "$PYTHON_BIN" -u market_history_worker.py --loop
  wait_for_service_stable market_history_worker 3
  CORE_SERVICES="$CORE_SERVICES market_history_worker"
fi

if [[ "$REVERSE_TUNNEL_ENABLED" == "1" ]]; then
  if [[ -z "$PUBLIC_SITE_URL" ]]; then
    PUBLIC_SITE_URL="$($PROJECT_DIR/start_reverse_tunnel.sh --public-url)"
  fi
  # This is this project's existing production domain.  It can be overridden
  # through PUBLIC_SITE_URL or the private hosting config.
  PUBLIC_SITE_URL="${PUBLIC_SITE_URL:-https://csskinsprediction.online}"
  PUBLIC_SITE_URL="${PUBLIC_SITE_URL%/}"
  if ! [[ "$PUBLIC_SITE_URL" =~ ^https?://[A-Za-z0-9._:-]+$ ]]; then
    echo "PUBLIC_SITE_URL must look like https://example.com (without a path)." >&2
    exit 1
  fi
  start_service reverse_tunnel env CS2_TUNNEL_NONINTERACTIVE=1 "$PROJECT_DIR/start_reverse_tunnel.sh"
  wait_for_service_stable reverse_tunnel 8
  if wait_for_public_site; then
    PUBLIC_SITE_VERIFIED=1
  else
    echo "warning: SSH is alive, but public /health could not be confirmed from this Mac" >&2
  fi
fi

if [[ "$START_PARSER" == "1" ]]; then
  start_service parser_final "$PYTHON_BIN" -u parser_final.py
  wait_for_service_stable parser_final 3
  CORE_SERVICES="$CORE_SERVICES parser_final"
fi
if [[ "$START_SCANNER" == "1" ]]; then
  start_service deal_scanner "$PYTHON_BIN" -u deal_scanner.py
  wait_for_service_stable deal_scanner 3
  CORE_SERVICES="$CORE_SERVICES deal_scanner"
fi

if [[ "$API_ENABLED" == "1" ]]; then
  API_HOST="$($PYTHON_BIN -c 'import app_config; print(app_config.get("API_HOST", "127.0.0.1"))')"
  API_PORT="$($PYTHON_BIN -c 'import app_config; print(app_config.get_int("API_PORT", 8000, minimum=1, maximum=65535))')"
  API_HEALTH_HOST="$API_HOST"
  if [[ "$API_HEALTH_HOST" == "0.0.0.0" || "$API_HEALTH_HOST" == "::" ]]; then
    API_HEALTH_HOST="127.0.0.1"
  fi
  API_HEALTH_URL="http://$API_HEALTH_HOST:$API_PORT/health"
  start_service api_server "$PYTHON_BIN" -u api_server.py
  wait_for_api
  CORE_SERVICES="$CORE_SERVICES api_server"
fi

if [[ "$TELEGRAM_ENABLED" == "1" ]]; then
  start_service telegram_bot "$PYTHON_BIN" -u tg_deals_bot.py
  wait_for_service_stable telegram_bot 3
  CORE_SERVICES="$CORE_SERVICES telegram_bot"
fi

if [[ "$PUBLIC_TUNNEL" == "1" ]]; then
  if [[ "$REVERSE_TUNNEL_ENABLED" == "1" ]]; then
    echo "warning: both permanent reverse SSH and temporary Quick Tunnel are enabled" >&2
  fi
  if ! command -v cloudflared >/dev/null 2>&1; then
    echo "warning: cloudflared is not installed; other services remain running" >&2
  else
    start_service public_tunnel cloudflared tunnel --url "http://$HEALTH_HOST:$DASHBOARD_PORT" --no-autoupdate --protocol http2
    if wait_for_quick_tunnel; then
      QUICK_TUNNEL_URL="$(tr -d '[:space:]' < "$RUN_DIR/public_url.txt")"
      QUICK_TUNNEL_VERIFIED=1
    else
      echo "warning: Quick Tunnel failed; other services remain running" >&2
      tunnel_pid="$(pid_from_file "$RUN_DIR/public_tunnel.pid")"
      is_running "$tunnel_pid" && kill "$tunnel_pid" >/dev/null 2>&1 || true
      rm -f "$RUN_DIR/public_tunnel.pid"
      STARTED_SERVICES="${STARTED_SERVICES// public_tunnel/}"
    fi
  fi
fi

DASHBOARD_TOKEN="$($PYTHON_BIN -c 'import dashboard_server; print(dashboard_server.DASHBOARD_TOKEN)')"
DASHBOARD_TOKEN_FILE_DISPLAY="$($PYTHON_BIN -c 'import dashboard_server; print(dashboard_server.TOKEN_PATH)')"
LOCAL_DASHBOARD_URL="http://$HEALTH_HOST:$DASHBOARD_PORT/?token=$DASHBOARD_TOKEN"
DASHBOARD_URL="$LOCAL_DASHBOARD_URL"
if [[ "$PUBLIC_SITE_VERIFIED" == "1" && -n "$PUBLIC_SITE_URL" ]]; then
  DASHBOARD_URL="$PUBLIC_SITE_URL/?token=$DASHBOARD_TOKEN"
elif [[ "$QUICK_TUNNEL_VERIFIED" == "1" && -n "$QUICK_TUNNEL_URL" ]]; then
  DASHBOARD_URL="$QUICK_TUNNEL_URL/?token=$DASHBOARD_TOKEN"
elif [[ "$REVERSE_TUNNEL_ENABLED" == "1" ]]; then
  echo "public site was not verified; opening the healthy local dashboard instead" >&2
fi

echo
echo "all configured services are running with: $PYTHON_BIN"
if [[ "$QUIET_DASHBOARD_URL" == "1" ]]; then
  echo "dashboard is running (private token URL hidden)"
  printf 'admin token file: %q\n' "$DASHBOARD_TOKEN_FILE_DISPLAY"
else
  echo "dashboard: $DASHBOARD_URL"
fi
echo "logs: $LOG_DIR"
echo "keep this terminal open; press Ctrl+C to stop services started here"

if [[ "$OPEN_BROWSER" == "1" ]] && command -v open >/dev/null 2>&1; then
  open "$DASHBOARD_URL"
fi

while true; do
  sleep 5

  if [[ "$REVERSE_TUNNEL_ENABLED" == "1" ]]; then
    reverse_pid="$(pid_from_file "$RUN_DIR/reverse_tunnel.pid")"
    if ! is_running "$reverse_pid"; then
      echo "reverse tunnel disconnected; reconnecting..." >&2
      start_service reverse_tunnel env CS2_TUNNEL_NONINTERACTIVE=1 "$PROJECT_DIR/start_reverse_tunnel.sh"
      if ! wait_for_service_stable reverse_tunnel 8; then
        echo "warning: reconnect failed; local services remain online and another attempt will follow" >&2
      fi
    fi
  fi

  for service_name in $CORE_SERVICES; do
    service_pid="$(pid_from_file "$RUN_DIR/$service_name.pid")"
    if [[ " $STARTED_SERVICES " == *" $service_name "* ]] && ! is_running "$service_pid"; then
      echo "$service_name stopped unexpectedly. Check $LOG_DIR/$service_name.log" >&2
      exit 1
    fi
  done
done
