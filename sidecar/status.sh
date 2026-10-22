#!/usr/bin/env bash
# Report the codex-sidecar unit state and the authenticated /healthz result on EVERY bind
# the sidecar is expected to hold — loopback and the docker0 gateway (172.17.0.1, the bind
# the router container uses). Probing loopback alone was blind to the boot-time docker0
# bind race by construction: that bind always works. Asserts on the body (`"status":"ok"`),
# never on the status code alone. Never prints SIDECAR_KEY. Exits non-zero when unhealthy.
#
# Then one fixture turn as an install-time POSTURE probe (WP-3, 2026-09-14): the declared
# feature block cannot remove codex's collab tools or code mode in codex-cli 0.154.0, so the
# posture is *probed*, not assumed — the response must carry a `stop_reason` and must contain
# no collabAgentToolCall / webSearch / commandExecution, and the turn's `config-verified` row
# must exist, whose `features` readings are printed. Body assertions only, never status codes.
for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '[--help]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

set -euo pipefail

INSTALL_HOME="${SIDECAR_INSTALL_HOME:-$HOME}"
# fleet-env cutover switch (plans/env-management.md). 0 = ON, 1 = OFF; exits loudly if
# ON but the fleet-env binary is missing or the switch is malformed (never a silent
# fallback to the legacy file).
FLEET_ENV="${FLEET_ENV_BIN:-<code-root>/fleet-tools/fleet/fleet_env/bin/fleet-env}"
fleet_env_on() {
  local rc=0
  if [[ ! -x "$FLEET_ENV" ]]; then
    case "${FLEET_ENV_CUTOVER:-}" in
      0) return 1 ;;
      1) ;;
      *) [[ -e "${FLEET_ENV_HOME:-$HOME/.config/fleet-env}/cutover" ]] || return 1 ;;
    esac
    printf '%s: fleet-env cutover is on but %s is missing; refusing the legacy env path\n' "$0" "$FLEET_ENV" >&2
    exit 1
  fi
  "$FLEET_ENV" enabled || rc=$?
  if [[ $rc -gt 1 ]]; then
    printf '%s: fleet-env enabled failed (exit %d)\n' "$0" "$rc" >&2
    exit 1
  fi
  return "$rc"
}

# Same choice as install.sh: the fleet-env projection under the cutover, else the legacy file.
if fleet_env_on; then
  ENV_FILE="$("$FLEET_ENV" path codex-sidecar)" || { printf 'fleet-env path codex-sidecar failed\n' >&2; exit 1; }
else
  ENV_FILE="$INSTALL_HOME/.config/codex-sidecar/env"
fi
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

# Same default as sidecar/rs/src/config.rs read_config(): unset → docker0 gateway; empty → no extra bind.
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

# ---------------------------------------------------------------- posture probe ----

printf '\n== posture probe (one fixture turn) ==\n'
probe_file="$(mktemp)"
trap 'rm -f -- "$body_file" "$probe_file"' EXIT

if ! command -v jq >/dev/null 2>&1; then
  printf 'posture probe skipped: jq not found\n' >&2
  exit 1
fi

# A fresh session id per run, so the probe's own log rows are findable among live traffic.
probe_sid="$(cat /proc/sys/kernel/random/uuid)"
probe_body="$(jq -nc --arg sid "$probe_sid" '{
  model: "gpt-5.6-luna",
  max_tokens: 64,
  stream: false,
  system: [{ type: "text", text: "You are a posture probe. Answer in one word." }],
  messages: [{ role: "user", content: "Reply with the single word ok." }],
  metadata: { user_id: ({ device_id: "status-probe", account_uuid: "status-probe", session_id: $sid } | tojson) }
}')"

curl -sS -o "$probe_file" \
  -H "authorization: Bearer ${key}" -H 'content-type: application/json' \
  --data-binary "$probe_body" \
  "http://127.0.0.1:${PORT}/codex-b/v1/messages" >/dev/null || true

if ! jq -e '.stop_reason != null' -- "$probe_file" >/dev/null 2>&1; then
  printf 'posture probe: no stop_reason in the response body (the turn did not complete)\n' >&2
  head -c 400 -- "$probe_file" >&2 || true
  printf '\n' >&2
  exit 1
fi
printf 'stop_reason: %s\n' "$(jq -r '.stop_reason' -- "$probe_file")"

if grep -qE 'collabAgentToolCall|webSearch|commandExecution' -- "$probe_file"; then
  printf 'posture probe: an out-of-band surface appeared in the response body\n' >&2
  exit 1
fi
printf 'no collabAgentToolCall / webSearch / commandExecution in the body\n'

# The declared posture as the app-server read it back, from this turn's own config-verified row.
log_dir="${SIDECAR_LOG_DIR:-$INSTALL_HOME/.local/state/codex-sidecar/logs}"
log_file="$log_dir/codex-sidecar-$(date -u +%F).jsonl"
if [ ! -f "$log_file" ]; then
  printf 'posture probe: no log file at %s\n' "$log_file" >&2
  exit 1
fi

tail_rows="$(tail -n 400 -- "$log_file" || true)"
thread_id="$(printf '%s\n' "$tail_rows" | jq -rs --arg sid "$probe_sid" \
  '[.[] | select(.kind == "http" and .key == $sid)] | last | .threadId // empty' 2>/dev/null || true)"
if [ -z "$thread_id" ]; then
  printf 'posture probe: no http log row for this probe session in %s\n' "$log_file" >&2
  exit 1
fi

features="$(printf '%s\n' "$tail_rows" | jq -cs --arg tid "$thread_id" \
  '[.[] | select(.kind == "config-verified" and .threadId == $tid)] | last | .features // empty' 2>/dev/null || true)"
if [ -z "$features" ]; then
  printf 'posture probe: no config-verified row carrying features for thread %s\n' "$thread_id" >&2
  exit 1
fi
printf 'thread:   %s\n' "$thread_id"
printf 'features: %s\n' "$features"

exit "$rc"
