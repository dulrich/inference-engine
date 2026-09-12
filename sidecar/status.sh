#!/usr/bin/env bash
# Report the codex-sidecar unit state and the authenticated /healthz result.
# Never prints SIDECAR_KEY. Exits non-zero when the sidecar is unhealthy.
set -euo pipefail

INSTALL_HOME="${SIDECAR_INSTALL_HOME:-$HOME}"
ENV_FILE="$INSTALL_HOME/.config/codex-sidecar/env"
PORT="${SIDECAR_PORT:-4877}"
URL="http://127.0.0.1:${PORT}/healthz"

printf '== systemctl --user status codex-sidecar ==\n'
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user status codex-sidecar --no-pager || true
else
  printf 'systemctl not found; skipping unit status\n'
fi

printf '\n== %s ==\n' "$URL"
if [ ! -f "$ENV_FILE" ]; then
  printf 'no env file at %s; run install.sh first\n' "$ENV_FILE" >&2
  exit 1
fi

# Read the key without exporting it into the environment of anything but curl.
key="$(sed -n 's/^SIDECAR_KEY=//p' -- "$ENV_FILE" | tail -n 1)"
if [ -z "$key" ]; then
  printf 'SIDECAR_KEY missing from %s\n' "$ENV_FILE" >&2
  exit 1
fi

body_file="$(mktemp)"
trap 'rm -f -- "$body_file"' EXIT

code="$(curl -sS -o "$body_file" -w '%{http_code}' \
  -H "authorization: Bearer ${key}" "$URL" || true)"

if [ -z "$code" ] || [ "$code" = "000" ]; then
  printf 'status: connection refused (is the unit running?)\n' >&2
  exit 1
fi

printf 'status: %s\n' "$code"
printf 'body:   '
cat -- "$body_file"
printf '\n'

if [ "$code" != "200" ]; then
  exit 1
fi
