#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${HOSTING_CONFIG_FILE:-$PROJECT_DIR/hosting.local.env}"
MODE="${1:-run}"

if (( $# > 1 )); then
  echo "Usage: $0 [--check|--public-url]" >&2
  exit 2
fi
case "$MODE" in
  run|--check|--public-url) ;;
  *)
    echo "Usage: $0 [--check|--public-url]" >&2
    exit 2
    ;;
esac

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing $CONFIG_FILE. Copy hosting.local.env.example and fill VPS_HOST." >&2
  exit 1
fi

# Keep VPS credentials out of all Python processes.  Parse this private file as
# data instead of sourcing shell code, so values may safely contain $, #, !, =.
VPS_HOST=""
VPS_USER=""
VPS_SSH_PORT=""
VPS_IDENTITY_FILE=""
VPS_PASSWORD=""
REMOTE_BIND_HOST=""
REMOTE_PORT=""
LOCAL_HOST=""
LOCAL_PORT=""
PUBLIC_SITE_URL=""
SEEN_KEYS=" "

while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  line="${raw_line%$'\r'}"
  line="${line#"${line%%[![:space:]]*}"}"
  [[ -z "$line" || "$line" == \#* ]] && continue
  if [[ "$line" != *=* ]]; then
    echo "Invalid line in $CONFIG_FILE: expected KEY=VALUE" >&2
    exit 1
  fi

  config_key="${line%%=*}"
  config_value="${line#*=}"
  if ! [[ "$config_key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
    echo "Invalid setting name in $CONFIG_FILE: $config_key" >&2
    exit 1
  fi
  if [[ "$SEEN_KEYS" == *" $config_key "* ]]; then
    echo "Duplicate setting in $CONFIG_FILE: $config_key" >&2
    exit 1
  fi
  SEEN_KEYS="$SEEN_KEYS$config_key "

  if [[ "$config_value" == \"*\" && "$config_value" == *\" && ${#config_value} -ge 2 ]]; then
    config_value="${config_value:1:${#config_value}-2}"
  elif [[ "$config_value" == \'*\' && "$config_value" == *\' && ${#config_value} -ge 2 ]]; then
    config_value="${config_value:1:${#config_value}-2}"
  fi

  case "$config_key" in
    VPS_HOST|VPS_USER|VPS_SSH_PORT|VPS_IDENTITY_FILE|VPS_PASSWORD|REMOTE_BIND_HOST|REMOTE_PORT|LOCAL_HOST|LOCAL_PORT|PUBLIC_SITE_URL)
      printf -v "$config_key" '%s' "$config_value"
      ;;
    *)
      echo "Unknown setting in $CONFIG_FILE: $config_key" >&2
      exit 1
      ;;
  esac
done < "$CONFIG_FILE"

config_mode="$(stat -f %Lp "$CONFIG_FILE" 2>/dev/null || stat -c %a "$CONFIG_FILE")"
if [[ "${config_mode: -2}" != "00" ]]; then
  echo "$CONFIG_FILE is readable by other users (mode $config_mode). Run: chmod 600 '$CONFIG_FILE'" >&2
  exit 1
fi

: "${VPS_HOST:?Set VPS_HOST in $CONFIG_FILE}"
VPS_USER="${VPS_USER:-root}"
VPS_SSH_PORT="${VPS_SSH_PORT:-22}"
REMOTE_BIND_HOST="${REMOTE_BIND_HOST:-127.0.0.1}"
REMOTE_PORT="${REMOTE_PORT:-8010}"
LOCAL_HOST="${LOCAL_HOST:-127.0.0.1}"
LOCAL_PORT="${LOCAL_PORT:-8010}"

if [[ "$VPS_HOST" == "YOUR_SERVER_IP" ]]; then
  echo "Replace YOUR_SERVER_IP in $CONFIG_FILE." >&2
  exit 1
fi
if [[ "$VPS_HOST" == *"://"* || "$VPS_HOST" == */* || "$VPS_HOST" == *[[:space:]]* ]]; then
  echo "VPS_HOST must contain only a hostname or IP address, without a URL scheme or spaces." >&2
  exit 1
fi
if [[ -z "$VPS_USER" || "$VPS_USER" == *[[:space:]]* ]]; then
  echo "VPS_USER must not be empty or contain spaces." >&2
  exit 1
fi

validate_port() {
  local name="$1"
  local value="$2"
  if ! [[ "$value" =~ ^[0-9]+$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
    echo "$name must be an integer from 1 to 65535." >&2
    exit 1
  fi
}

validate_port VPS_SSH_PORT "$VPS_SSH_PORT"
validate_port REMOTE_PORT "$REMOTE_PORT"
validate_port LOCAL_PORT "$LOCAL_PORT"

if [[ "$REMOTE_BIND_HOST" == *[[:space:]]* || "$LOCAL_HOST" == *[[:space:]]* ]]; then
  echo "Tunnel bind hosts must not contain spaces." >&2
  exit 1
fi

RESOLVED_IDENTITY_FILE=""
if [[ -n "$VPS_IDENTITY_FILE" ]]; then
  RESOLVED_IDENTITY_FILE="${VPS_IDENTITY_FILE/#\~/$HOME}"
  if [[ ! -f "$RESOLVED_IDENTITY_FILE" || ! -r "$RESOLVED_IDENTITY_FILE" ]]; then
    echo "VPS_IDENTITY_FILE does not point to a readable private key." >&2
    exit 1
  fi
fi

if [[ -n "$PUBLIC_SITE_URL" ]]; then
  PUBLIC_SITE_URL="${PUBLIC_SITE_URL%/}"
  if ! [[ "$PUBLIC_SITE_URL" =~ ^https?://[A-Za-z0-9._:-]+$ ]]; then
    echo "PUBLIC_SITE_URL must look like https://example.com (without a path)." >&2
    exit 1
  fi
fi

PASSWORD_HELPER=""
if [[ -n "$VPS_PASSWORD" ]]; then
  if command -v sshpass >/dev/null 2>&1; then
    PASSWORD_HELPER="sshpass"
  elif command -v expect >/dev/null 2>&1; then
    PASSWORD_HELPER="expect"
  elif [[ -z "$RESOLVED_IDENTITY_FILE" ]]; then
    echo "VPS_PASSWORD is set, but neither sshpass nor expect is available." >&2
    exit 1
  else
    echo "warning: no password helper; the configured SSH key will be used" >&2
    VPS_PASSWORD=""
  fi
fi
if [[ -z "$RESOLVED_IDENTITY_FILE" && -z "$VPS_PASSWORD" ]]; then
  echo "Set VPS_IDENTITY_FILE (recommended) or VPS_PASSWORD for one-command startup." >&2
  exit 1
fi

if [[ "$MODE" == "--check" ]]; then
  echo "reverse tunnel configuration: ready"
  exit 0
fi
if [[ "$MODE" == "--public-url" ]]; then
  printf '%s\n' "$PUBLIC_SITE_URL"
  exit 0
fi

SSH_COMMAND=(ssh)
# autossh is useful with key auth.  Password tunnels are restarted by the
# parent supervisor so expect/sshpass can authenticate each new connection.
if [[ -z "$VPS_PASSWORD" ]] && command -v autossh >/dev/null 2>&1; then
  SSH_COMMAND=(autossh -M 0)
  export AUTOSSH_GATETIME=0
fi

SSH_ARGS=(
  -N -T
  -p "$VPS_SSH_PORT"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -o StrictHostKeyChecking=accept-new
  -R "$REMOTE_BIND_HOST:$REMOTE_PORT:$LOCAL_HOST:$LOCAL_PORT"
)
if [[ -n "$RESOLVED_IDENTITY_FILE" ]]; then
  SSH_ARGS+=( -i "$RESOLVED_IDENTITY_FILE" )
fi
if [[ "${CS2_TUNNEL_NONINTERACTIVE:-0}" == "1" && -z "$VPS_PASSWORD" ]]; then
  SSH_ARGS+=( -o BatchMode=yes )
fi
SSH_ARGS+=( "$VPS_USER@$VPS_HOST" )

echo "reverse tunnel: VPS $REMOTE_BIND_HOST:$REMOTE_PORT -> Mac $LOCAL_HOST:$LOCAL_PORT"
if [[ "$PASSWORD_HELPER" == "sshpass" ]]; then
  export SSHPASS="$VPS_PASSWORD"
  exec sshpass -e "${SSH_COMMAND[@]}" "${SSH_ARGS[@]}"
fi
if [[ "$PASSWORD_HELPER" == "expect" ]]; then
  export CS2_SSH_PROGRAM="${SSH_COMMAND[0]}"
  export CS2_VPS_PASSWORD="$VPS_PASSWORD"
  export CS2_VPS_HOST="$VPS_HOST"
  export CS2_VPS_USER="$VPS_USER"
  export CS2_VPS_SSH_PORT="$VPS_SSH_PORT"
  export CS2_REMOTE_BIND_HOST="$REMOTE_BIND_HOST"
  export CS2_REMOTE_PORT="$REMOTE_PORT"
  export CS2_LOCAL_HOST="$LOCAL_HOST"
  export CS2_LOCAL_PORT="$LOCAL_PORT"
  export CS2_VPS_IDENTITY_FILE="$RESOLVED_IDENTITY_FILE"
  exec expect <<'EXPECT_SCRIPT'
set timeout -1
set tunnel_password $env(CS2_VPS_PASSWORD)
unset env(CS2_VPS_PASSWORD)
set command [list $env(CS2_SSH_PROGRAM) -N -T \
    -p $env(CS2_VPS_SSH_PORT) \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new \
    -R "$env(CS2_REMOTE_BIND_HOST):$env(CS2_REMOTE_PORT):$env(CS2_LOCAL_HOST):$env(CS2_LOCAL_PORT)"]
if {$env(CS2_VPS_IDENTITY_FILE) ne ""} {
    lappend command -i $env(CS2_VPS_IDENTITY_FILE)
}
lappend command "$env(CS2_VPS_USER)@$env(CS2_VPS_HOST)"
spawn -noecho {*}$command
trap {
    catch {close -i $spawn_id}
    catch {wait -i $spawn_id}
    exit 0
} {SIGINT SIGTERM}
set password_attempts 0
expect {
    -re {(?i)password: *$} {
        incr password_attempts
        if {$password_attempts > 1} {
            puts stderr "SSH rejected the configured VPS password."
            exit 1
        }
        send -- "$tunnel_password\r"
        exp_continue
    }
    eof {
        catch wait result
        if {[llength $result] >= 4} {
            exit [lindex $result 3]
        }
        exit 1
    }
}
EXPECT_SCRIPT
fi

exec "${SSH_COMMAND[@]}" "${SSH_ARGS[@]}"
