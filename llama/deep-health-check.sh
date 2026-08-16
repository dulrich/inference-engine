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
# Scheduling: NOT currently installed anywhere (no cron entry, no systemd timer, no compose
# sidecar) — scheduling is deferred by design; this script is complete and useful run by hand,
# which is its only invocation path today. Until it is scheduled, the outage-detection gap this
# script exists to close is still open: nothing runs it unless a human does. See
# llama/README.md's "Deep health check" section. When it is wired up, the recommended line is:
#
#   17 * * * * mkdir -p $HOME/.local/state/inference-engine && llama/deep-health-check.sh >>$HOME/.local/state/inference-engine/deep-health-check.log 2>&1
#
# Logs under $HOME/.local/state, NOT /var/log: this is a user crontab, /var/log is root-owned
# here, and a redirection into an unwritable directory fails before the script is ever exec'd —
# which would make this guardrail silently not run, the exact failure class it exists to catch.
#
# Hourly, and off the top of the hour on purpose (:17) to avoid herding with other jobs. The
# interval MUST exceed config.yaml's `ttl: 1800` (30 min) for bge-small, or the previous run's
# request would keep resetting the idle timer and every "cold spawn" would actually hit an
# already-warm process — proving nothing. Hourly clears that ttl with margin.
set -u

URL="http://127.0.0.1:4811"
MODEL="bge-small"

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

for bin in curl jq; do
	if ! command -v "$bin" >/dev/null 2>&1; then
		echo "[deep-health-check] FATAL: '$bin' is required but not on PATH." >&2
		exit 2
	fi
done

note() { echo "[deep-health-check] $*"; }

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

if probe_once >/dev/null; then
	note "PASS on first attempt"
	echo "[deep-health-check] PASS: $URL served a $MODEL embedding with 384 finite numbers."
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

if probe_once >/dev/null; then
	note "PASS on retry"
	echo "[deep-health-check] PASS: $URL served a $MODEL embedding with 384 finite numbers (after one retry)."
	exit 0
fi

echo "[deep-health-check] FAIL: $URL did not serve a well-formed $MODEL embedding after 2 attempts." >&2
exit 1
