#!/usr/bin/env bash
# Host-run deep spawn test for the crucible-inference llama-swap router (config.yaml in this
# dir). Proves the GPU tier can actually SPAWN A MODEL AND SERVE REAL NUMBERS — not just that
# the router process answers HTTP.
#
# Why this exists: for ~30 hours in August 2026 a glibc ABI skew killed every llama-server
# child at dynamic-link time, but llama-swap is a static Go binary, so it kept answering
# `/health` with `OK` throughout and `docker compose ps` said (healthy) the entire time. The
# compose healthcheck (compose.fleet.yml) only ever proves "router up" — it is deliberately kept
# shallow and fast (30s interval) because bge-small-en-v1.5-f16 has `ttl: 1800` in config.yaml,
# and any probe interval under 1800s resets the idle timer and never lets the model unload, so
# it could never observe a genuine cold spawn even if it tried. Hence the split: the compose
# healthcheck stays shallow, and THIS script — run separately, on its own schedule, slower than
# the ttl — is what proves a model can actually spawn and return real numbers. If you are
# reading this because it looked noisy and you were about to delete it: don't. It is the thing
# the 30-hour outage did not have.
#
# This is the fleet's central quirk applied here: LM Studio answers unknown endpoints with
# HTTP 200 and an error body, and llama-swap answered /health with OK through a total outage —
# so this script asserts on the response BODY, never a status code.
#
# Runs on the host, not in the container: the image has neither python3 nor jq, and its
# exec-form CMD cannot express a pipeline. Requires: curl, jq.
#
# Usage:
#   llama/deep-health-check.sh [--url http://127.0.0.1:4811] [--model bge-small]
#
# --model exists primarily so the negative case (a deliberately bogus alias) can be exercised
# without editing this file — see the gate that requires an observed FAILURE, not just a pass.
#
# Scheduling: INSTALLED 2026-08-24 in the user crontab (see llama/README.md "Deep health check"):
#
#   19 * * * * mkdir -p $HOME/.local/state/inference-engine && llama/deep-health-check.sh >>$HOME/.local/state/inference-engine/deep-health-check.log 2>&1
#
# Alerting: the log above is human-readable and nothing reads it. So every exit path — PASS,
# FAIL, SKIP and FATAL — also writes a machine-readable, timestamped artifact (atomic rename) at
# DEEP_HEALTH_ARTIFACT_PATH (default $HOME/.local/state/inference-engine/deep-health-check.json).
# Command Center (fleet-tools) reads it and renders a verdict on the `inference-engine-router`
# service card; an artifact older than its staleness bound, missing, or malformed renders
# non-healthy there, so a dead cron cannot leave a stale PASS on screen. SKIP is written too, on
# purpose: a multi-hour bake-off window must keep the timestamp fresh or it would read as stale.
#
# Logs under $HOME/.local/state, NOT /var/log: this is a user crontab, /var/log is root-owned
# here, and a redirection into an unwritable directory fails before the script is ever exec'd —
# which would make this guardrail silently not run, the exact failure class it exists to catch.
#
# Hourly, and off the top of the hour on purpose (:19 — :17 belongs to the image security
# scan; user decision 2026-08-24) to avoid herding with other jobs. The
# interval MUST exceed config.yaml's `ttl: 1800` (30 min) for bge-small, or the previous run's
# request would keep resetting the idle timer and every "cold spawn" would actually hit an
# already-warm process — proving nothing. Hourly clears that ttl with margin.
for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '[--url <url>] [--model <alias>]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../scripts/lib/build-id.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

set -u

URL="http://127.0.0.1:4811"
MODEL="bge-small"
ARTIFACT="${DEEP_HEALTH_ARTIFACT_PATH:-${HOME}/.local/state/inference-engine/deep-health-check.json}"
ATTEMPTS=0

while [ "$#" -gt 0 ]; do
	case "$1" in
	--url)
		URL="$2"
		shift 2
		;;
	--model)
		MODEL="$2"
		shift 2
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 2
		;;
	esac
done

note() { echo "[deep-health-check] $*"; }

# Minimal JSON string escaping (backslash, quote, control chars) — jq may be the very thing
# that is missing when the FATAL path runs, so the artifact writer must not depend on it.
json_str() {
	printf '%s' "$1" | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' -e 's/\r/\\r/g' | tr '\n' ' '
}

# write_artifact STATUS DETAIL — always best-effort: an unwritable artifact must never change
# the exit code of the check itself (the log line and exit status remain the primary record).
write_artifact() {
	local status="$1" detail="$2" dir tmp
	dir=$(dirname "$ARTIFACT")
	mkdir -p "$dir" 2>/dev/null || { note "WARN: cannot create $dir; artifact not written" >&2; return 0; }
	tmp=$(mktemp "$dir/.deep-health-check.XXXXXX" 2>/dev/null) || { note "WARN: cannot create temp file in $dir; artifact not written" >&2; return 0; }
	if printf '{"schema":1,"observed_at":"%s","status":"%s","url":"%s","model":"%s","attempts":%d,"detail":"%s"}\n' \
		"$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" "$(json_str "$URL")" "$(json_str "$MODEL")" "$ATTEMPTS" "$(json_str "$detail")" >"$tmp" \
		&& chmod 0644 "$tmp" && mv -f "$tmp" "$ARTIFACT"; then
		return 0
	fi
	rm -f "$tmp"
	note "WARN: artifact write to $ARTIFACT failed" >&2
	return 0
}

for bin in curl jq; do
	if ! command -v "$bin" >/dev/null 2>&1; then
		echo "[deep-health-check] FATAL: '$bin' is required but not on PATH." >&2
		write_artifact fatal "'$bin' is required but not on PATH"
		exit 2
	fi
done

# Bakeoff-window guard: a live bake-off run window `docker stop`s the router container for the
# whole window (batch.py's GPU maintenance window -- scratch servers run in a sibling container),
# so probing :4811 mid-window is guaranteed to read as an outage that isn't one. The window's
# whole lifetime is exactly the span its wrapper (the research harness's run-window.sh, eval-harness/inference_research/harness; cron.lock is the cross-repo interface) holds an exclusive
# flock on cron.lock, so "can I take the lock?" IS "is the router intentionally down?". The probe
# takes and releases it instantly (`true`) rather than holding it for the check's duration --
# holding it would make a concurrently-starting window's own `flock -n` read busy and get its
# start request `rejected(busy)`, a user-visible false rejection; the cost of instant release is
# a benign TOCTOU (a window starting mid-check), which the single-retry tolerance below absorbs.
# Skip is exit 0 on purpose: an intentionally-stopped router is not an alarm.
BAKEOFF_CRON_LOCK="${BAKEOFF_CRON_LOCK:-${HOME}/.local/state/inference-bakeoff/cron.lock}"
if [ -e "${BAKEOFF_CRON_LOCK}" ] && command -v flock >/dev/null 2>&1 \
	&& ! flock -n "${BAKEOFF_CRON_LOCK}" true 2>/dev/null; then
	note "SKIP: bake-off run window live (cron.lock held) -- router is intentionally stopped for the window"
	write_artifact skip "bake-off run window live (cron.lock held); router intentionally stopped"
	exit 0
fi

# 150s: llama-swap's own healthCheckTimeout (config.yaml) bounds a cold spawn's wait at 120s,
# so the curl timeout must exceed that or a genuine, healthy cold start reads as a false alarm.
MAX_TIME=150

# One probe attempt. Prints the response body to stdout on success (well-formed vector) and
# returns 0; prints nothing to stdout and returns 1 otherwise. Diagnostic detail goes to stderr
# via `note` so both a pass and a fail are legible in cron mail.
probe_once() {
	local json len
	json=$(curl -s --max-time "$MAX_TIME" "$URL/v1/embeddings" \
		-H 'Content-Type: application/json' \
		-d "$(printf '{"model":"%s","input":"deep-health-check probe"}' "$MODEL")") || json=""
	if [ -z "$json" ]; then
		note "empty response body (curl failed or timed out after ${MAX_TIME}s)" >&2
		return 1
	fi
	len=$(echo "$json" | jq -r '
		(.data[0].embedding // empty)
		| if type == "array" and length > 0
		     and (all(.[]; type == "number" and (isnan | not) and (isinfinite | not)))
		  then length else empty end
	' 2>/dev/null)
	if [ "$len" = "384" ]; then
		note "response body: data[0].embedding is a 384-length array of finite numbers" >&2
		echo "$json"
		return 0
	fi
	# Truncate the echoed body. A wrong-length-but-valid response (e.g. pointing this at
	# bge-m3, which returns 1024 dims) would otherwise dump a full embedding vector — ~44KB —
	# into cron mail or the log on every failure. The first 300 characters are enough to show
	# an error body, which is the case that actually needs reading.
	note "response did not contain a well-formed 384-length finite numeric vector. Got length: '${len:-none}'. Response (first 300 chars): $(printf '%.300s' "$json")" >&2
	return 1
}

note "probing POST $URL/v1/embeddings (model=$MODEL), asserting exactly 384 finite numbers in data[0].embedding ..."
note "generous timeout (${MAX_TIME}s): a genuine cold spawn loads a GGUF from disk and can take tens of seconds; llama-swap holds the request until the child is healthy."

ATTEMPTS=1
if probe_once >/dev/null; then
	note "PASS on first attempt"
	echo "[deep-health-check] PASS: $URL served a $MODEL embedding with 384 finite numbers."
	write_artifact pass "384 finite numbers on first attempt"
	exit 0
fi

# Retry once, and only once, before declaring failure. This is NOT flake-tolerance for its own
# sake: the `chat` group in config.yaml is `exclusive: true`, which evicts every other group's
# members (including bge-small, which lives in `retrieval`) the moment a chat request lands —
# so a chat request racing this probe can legitimately kill an in-flight spawn. That is the
# system working as designed, not a bug in either the router or this script. Do not "simplify"
# this retry away; a future reader who removes it will turn a normal eviction race into a
# false page.
note "first attempt failed — retrying once (the chat group's exclusive:true eviction can legitimately kill one in-flight probe) ..."
sleep 5

ATTEMPTS=2
if probe_once >/dev/null; then
	note "PASS on retry"
	echo "[deep-health-check] PASS: $URL served a $MODEL embedding with 384 finite numbers (after one retry)."
	write_artifact pass "384 finite numbers after one retry"
	exit 0
fi

echo "[deep-health-check] FAIL: $URL did not serve a well-formed $MODEL embedding after 2 attempts." >&2
write_artifact fail "no well-formed $MODEL embedding after 2 attempts"
exit 1
