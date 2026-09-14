#!/usr/bin/env bash
# Report the codex-sidecar unit state and the authenticated /healthz result on EVERY bind
# the sidecar is expected to hold — loopback and the docker0 gateway (172.17.0.1, the bind
# the router container uses). Probing loopback alone was blind to the boot-time docker0
# bind race by construction: that bind always works. Asserts on the body (`"status":"ok"`),
# never on the status code alone. Never prints SIDECAR_KEY. Exits non-zero when unhealthy.
set -euo pipefail

INSTALL_HOME="${SIDECAR_INSTALL_HOME:-$HOME}"
ENV_FILE="$INSTALL_HOME/.config/codex-sidecar/env"
PORT="${SIDECAR_PORT:-4877}"

printf '== systemctl --user status codex-sidecar ==\n'
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user status codex-sidecar --no-pager || true
else
  printf 'systemctl not found; skipping unit status\n'
fi

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

# Same default as codex-sidecar.mjs readConfig(): unset → docker0 gateway; empty → no extra bind.
if grep -q '^SIDECAR_BIND_EXTRA=' -- "$ENV_FILE"; then
  extra="$(sed -n 's/^SIDECAR_BIND_EXTRA=//p' -- "$ENV_FILE" | tail -n 1)"
else
  extra="${SIDECAR_BIND_EXTRA-172.17.0.1}"
fi
hosts="127.0.0.1"
if [ -n "$extra" ]; then hosts="$hosts $extra"; fi

body_file="$(mktemp)"
trap 'rm -f -- "$body_file"' EXIT

rc=0
for host in $hosts; do
  url="http://${host}:${PORT}/healthz"
  printf '\n== %s ==\n' "$url"
  code="$(curl -sS -o "$body_file" -w '%{http_code}' \
    -H "authorization: Bearer ${key}" "$url" || true)"

  if [ -z "$code" ] || [ "$code" = "000" ]; then
    printf 'status: connection refused (unit not running, or this bind was lost)\n' >&2
    rc=1
    continue
  fi

  printf 'status: %s\n' "$code"
  printf 'body:   '
  cat -- "$body_file"
  printf '\n'

  if [ "$code" != "200" ] || ! grep -q '"status":"ok"' -- "$body_file"; then
    rc=1
  fi
done

exit "$rc"
