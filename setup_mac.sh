#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BOOTSTRAP="${PYTHON_BOOTSTRAP:-$(command -v python3.12 || true)}"
if [[ -z "$PYTHON_BOOTSTRAP" ]]; then
  echo "Python 3.12 was not found." >&2
  echo "Install it with: brew install python@3.12" >&2
  exit 1
fi

if [[ -d .venv && ! -x .venv/bin/python ]]; then
  echo ".venv exists but is not a macOS Python environment." >&2
  echo "Rename it first: mv .venv .venv.from-windows" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Creating .venv with $PYTHON_BOOTSTRAP"
  "$PYTHON_BOOTSTRAP" -m venv .venv
fi

./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -r requirements-dev.txt

if [[ ! -f config.local.env ]]; then
  cp config.local.env.example config.local.env
fi
if [[ ! -f hosting.local.env ]]; then
  cp hosting.local.env.example hosting.local.env
fi

chmod 600 config.local.env hosting.local.env
for secret_file in \
  key key2 key3 key4 key_for_friend api_keys_proxies.txt \
  dashboard_token.txt telegram_bot_token.txt steamwebapi_key.txt api_token.txt
do
  [[ -f "$secret_file" ]] && chmod 600 "$secret_file"
done
chmod 700 start_all.sh start_reverse_tunnel.sh setup_mac.sh

./.venv/bin/python -m pip check
./.venv/bin/python -m pytest -q

echo
echo "Setup complete. Start the full configured project with:"
echo "./start_all.sh"
