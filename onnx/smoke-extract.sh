#!/usr/bin/env bash
# Host-run smoke test for inference-engine-onnx (the GLiNER2-family CPU sidecar).
#
# Run this AFTER the container is up. It does not start, stop, or otherwise touch any container.
#
# Usage:
#   onnx/smoke-extract.sh [--url http://127.0.0.1:4812] [--models "gliner2-base gliner2-pii"] [--slow]
#
# Discipline, inherited from llama/smoke-inference.sh and worth restating because it is what
# makes this file worth running:
#
#   * every assertion is on the RESPONSE BODY, never on the status code alone. This fleet has
#     been burned twice by services that answer 200 with an error payload, and once by a
#     capability probe that read a status code (llama/AGENTS.md). A status code is checked only
#     where the *code itself* is the contract — the unknown-alias check below.
#   * assertions are about SHAPE, not values. "Nairobi is a location" is a model quality claim
#     and would make this script fail on a model swap; "every entity list element is an object
#     with a string `text` and a numeric `confidence`" is the wire contract and must hold for
#     any member of the family.
#   * a check that is too slow to run by default is SKIPPED AND REPORTED, never silently
#     dropped (see --slow).
#
# Exits non-zero on any check failure. Requires: curl, jq.
for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '[--url <url>] [--models "<alias> ..."] [--slow]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../scripts/lib/build-id.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

set -u

URL="http://127.0.0.1:4812"
MODELS="gliner2-base gliner2-pii"
SLOW=0

while [ "$#" -gt 0 ]; do
	case "$1" in
	--url)
		URL="$2"
		shift 2
		;;
	--models)
		MODELS="$2"
		shift 2
		;;
	--slow)
		SLOW=1
		shift
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 2
		;;
	esac
done

for bin in curl jq; do
	if ! command -v "$bin" >/dev/null 2>&1; then
		echo "[smoke] FATAL: '$bin' is required but not on PATH." >&2
		exit 2
	fi
done

fail=0
note() { echo "[smoke] $*"; }
pass() { echo "[smoke] PASS: $*"; }
bad() {
	echo "[smoke] FAIL: $*" >&2
	fail=1
}

# Per-run temp dir: isolates concurrent runs (no shared /tmp paths) and is removed on exit
# regardless of how the script exits. SMOKE_TMPDIR is unset until this line runs, so the trap
# must tolerate that under `set -u` if the script dies before it gets here.
SMOKE_TMPDIR=""
cleanup() {
	# shellcheck disable=SC2317  # reachable via `trap cleanup EXIT` below, not dead code
	[ -n "$SMOKE_TMPDIR" ] && rm -rf "$SMOKE_TMPDIR"
}
trap cleanup EXIT
SMOKE_TMPDIR=$(mktemp -d) || {
	echo "[smoke] FATAL: mktemp -d failed." >&2
	exit 2
}

# The crisis24-shaped schema news-ingestion actually sends: 7 described entity labels plus two
# single-label classifications. Kept here as the smoke's fixture because a schema nobody sends
# proves nothing — but note the labels are the CLIENT's, supplied per request; the service
# stores no label set of its own.
read -r -d '' SCHEMA <<'JSON'
{
  "entities": {
    "person": "Names of people or named individuals",
    "organization": "Companies, agencies, militant groups, or other named organizations",
    "location": "Cities, regions, countries, or named places",
    "facility": "Named buildings, infrastructure, airports, or installations",
    "weapon": "Weapons, ordnance, or means of attack",
    "casualties": "Mentions of deaths, injuries, or people affected",
    "date": "Dates, times, or explicit temporal references"
  },
  "classifications": [
    {"name": "severity", "labels": ["Info", "Caution", "Warning", "Critical"]},
    {"name": "event_type", "labels": ["disaster", "attack", "protest", "threat", "political", "economic", "unrest"]}
  ]
}
JSON

TEXT_ONE="Security forces in Nairobi clashed with demonstrators near Jomo Kenyatta International Airport on 14 March 2026 after the Kenyan National Police Service used tear gas and live rounds against a protest organised by the Wananchi Movement. At least three people were killed and twenty-seven injured, according to the Kenya Red Cross Society."
TEXT_TWO="Heavy rainfall flooded the A4 motorway outside Lisbon on Tuesday; no injuries were reported."

# ── 1. GET /health — per-model readiness, read from the body ────────────────────────────────
note "checking GET /health ..."
health_json=$(curl -s --max-time 15 "$URL/health") || health_json=""
health_ok=$(echo "$health_json" | jq -r '(.ok // empty) | if type == "boolean" then tostring else empty end' 2>/dev/null)
if [ "$health_ok" = "true" ]; then
	pass "/health reports ok:true"
elif [ "$health_ok" = "false" ]; then
	bad "/health reports ok:false (a preload model is not resident). Response: $health_json"
else
	bad "/health did not return a boolean .ok. Response: $health_json"
fi

for model in $MODELS; do
	entry=$(echo "$health_json" | jq -c --arg m "$model" '.models[$m] // empty' 2>/dev/null)
	loaded=$(echo "$entry" | jq -r '.loaded | tostring' 2>/dev/null)
	preload=$(echo "$entry" | jq -r '.preload | tostring' 2>/dev/null)
	if [ -z "$entry" ]; then
		bad "/health: model '$model' is absent from .models"
	elif [ "$loaded" = "true" ]; then
		load_s=$(echo "$entry" | jq -r '.load_seconds // "n/a"')
		pass "/health: model '$model' loaded (cold load was ${load_s}s)"
	elif [ "$preload" = "false" ] && [ "$loaded" = "false" ] && [ "$(echo "$entry" | jq -r '.error // empty')" = "" ]; then
		# Lazy alias (preload:false, idle unload): cold is a valid state; the extract checks
		# below load it on first request.
		pass "/health: model '$model' is lazy and currently unloaded (loads on first request)"
	else
		bad "/health: model '$model' is not loaded. Entry: ${entry}"
	fi
done

# ── 2. GET /v1/models lists every alias under test ──────────────────────────────────────────
note "checking GET /v1/models ..."
models_json=$(curl -s --max-time 15 "$URL/v1/models") || models_json=""
listed=$(echo "$models_json" | jq -r '.data[]?.id // empty' 2>/dev/null)
for model in $MODELS; do
	if echo "$listed" | grep -qx "$model"; then
		pass "/v1/models lists '$model'"
	else
		bad "/v1/models does not list '$model'. Got: $(echo "$listed" | tr '\n' ',')"
	fi
done

# ── 3. POST /v1/extract — one shape check per configured model ──────────────────────────────
# Asserted: two results for two texts; `entities` carries exactly the requested labels; every
# entity is {text: string, confidence: number}; each classification is {label, confidence} with
# the label drawn from the labels the REQUEST supplied. Nothing here asserts which entity the
# model found — that is a quality question, not a wire question.
for model in $MODELS; do
	note "checking POST /v1/extract (model=$model, 2 texts) ..."
	body=$(jq -n --arg m "$model" --arg t1 "$TEXT_ONE" --arg t2 "$TEXT_TWO" --argjson s "$SCHEMA" \
		'{model: $m, texts: [$t1, $t2], schema: $s}')
	extract_json=$(curl -s --max-time 120 "$URL/v1/extract" \
		-H 'Content-Type: application/json' -d "$body") || extract_json=""
	check=$(echo "$extract_json" | jq -r --argjson s "$SCHEMA" '
		($s.entities | keys_unsorted) as $elabels
		| $s.classifications as $cls
		| (.results // empty) as $r
		| if ($r | type) != "array" then "bad:not-an-array"
		  elif ($r | length) != 2 then "bad:length"
		  elif any($r[]; (.entities | type) != "object") then "bad:entities-type"
		  elif any($r[]; (.entities | keys_unsorted) != $elabels) then "bad:entity-labels"
		  elif any($r[]; .entities | to_entries[] | (.value | type) != "array") then "bad:entity-list"
		  elif any($r[]; .entities | to_entries[] | .value[] | ((.text | type) != "string") or ((.confidence | type) != "number")) then "bad:entity-span-shape"
		  elif any($r[]; . as $x | $cls[] | ($x[.name] | type) != "object") then "bad:classification-missing"
		  elif any($r[]; . as $x | $cls[] | ($x[.name].confidence | type) != "number") then "bad:classification-confidence"
		  elif any($r[]; . as $x | $cls[] as $c | ($x[$c.name].label | IN($c.labels[])) | not) then "bad:classification-label-not-in-request"
		  else "ok"
		  end
	' 2>/dev/null)
	if [ "$check" = "ok" ]; then
		n_ents=$(echo "$extract_json" | jq -r '[.results[0].entities[][]] | length')
		sev=$(echo "$extract_json" | jq -r '.results[0].severity.label')
		pass "/v1/extract ($model): 2 results, gliner2-native shape, ${n_ents} entities on text 1, severity=$sev"
	else
		bad "/v1/extract ($model) failed shape validation (${check:-no parseable response}). Response: $(echo "$extract_json" | head -c 600)"
	fi
done

# ── 4. NEGATIVE: an unknown alias must fail FAST with 4xx and a JSON error body ─────────────
# This is the one place the status code IS the contract: consumers map 5xx to "service down"
# and retry, so a typo'd model name must be a 4xx. And it must never quietly answer from some
# default model — that would write features from the wrong model under the right model id.
note "checking POST /v1/extract with an unknown alias fails fast with 4xx ..."
start_ts=$(date +%s)
unknown_body=$(jq -n --argjson s "$SCHEMA" '{model: "definitely-not-a-real-alias", texts: ["hello"], schema: $s}')
unknown_out=$(curl -s -o "$SMOKE_TMPDIR/onnx-smoke-unknown.json" -w '%{http_code}' --max-time 10 \
	"$URL/v1/extract" -H 'Content-Type: application/json' -d "$unknown_body")
curl_rc=$?
elapsed=$(($(date +%s) - start_ts))
unknown_json=$(cat "$SMOKE_TMPDIR/onnx-smoke-unknown.json" 2>/dev/null)
err_code=$(echo "$unknown_json" | jq -r '.error.code // empty' 2>/dev/null)
has_results=$(echo "$unknown_json" | jq -r 'has("results")' 2>/dev/null)
if [ "$curl_rc" -ne 0 ]; then
	bad "unknown-alias request failed/timed out (rc=$curl_rc) after ${elapsed}s — expected a fast 4xx"
elif [ "$elapsed" -gt 5 ]; then
	bad "unknown-alias request took ${elapsed}s (> 5s) — expected a fast failure"
elif [ "$has_results" = "true" ]; then
	bad "unknown-alias request returned RESULTS — the service fell back to a default model. Response: $unknown_json"
elif ! echo "$unknown_out" | grep -qE '^4[0-9][0-9]$'; then
	bad "unknown-alias request returned $unknown_out (expected 4xx; 5xx reads as 'service down' to consumers). Response: $unknown_json"
elif [ "$err_code" != "unknown_model" ]; then
	bad "unknown-alias request returned $unknown_out but error.code was '${err_code:-absent}' (expected 'unknown_model')"
else
	pass "unknown alias -> $unknown_out error.code=unknown_model in ${elapsed}s, no results"
fi

# ── 5. NEGATIVE: an unsupported schema shape must be a 400, not a wrong answer ──────────────
note "checking POST /v1/extract with an unsupported schema key is rejected ..."
rel_body=$(jq -n '{model: "'"${MODELS%% *}"'", texts: ["hello"], schema: {relations: ["employed_by"]}}')
rel_out=$(curl -s -o "$SMOKE_TMPDIR/onnx-smoke-rel.json" -w '%{http_code}' --max-time 10 \
	"$URL/v1/extract" -H 'Content-Type: application/json' -d "$rel_body")
rel_json=$(cat "$SMOKE_TMPDIR/onnx-smoke-rel.json" 2>/dev/null)
rel_code=$(echo "$rel_json" | jq -r '.error.code // empty' 2>/dev/null)
if [ "$rel_code" = "bad_schema" ] && echo "$rel_out" | grep -qE '^4[0-9][0-9]$'; then
	pass "unsupported schema key -> $rel_out error.code=bad_schema"
else
	bad "unsupported schema key returned $rel_out / error.code='${rel_code:-absent}' (expected 4xx bad_schema). Response: $rel_json"
fi

# ── 6. NEGATIVE: a malformed cls_threshold must be a 400 with a body, not a dropped connection ─
# Before the WP-R1 fix, {"cls_threshold": null} made the server drop the connection with NO HTTP
# response at all (curl succeeds, body empty) instead of answering with an error. A wrong status
# is not the regression this guards against — an empty/absent reply is, so this asserts all three
# of: 4xx status, error.code == bad_schema, and a response was actually received (curl's exit
# code is 0 and the body is non-empty).
note "checking POST /v1/extract with a malformed cls_threshold gets a real 400 response (not a dropped connection) ..."
cls_body=$(jq -n '{model: "'"${MODELS%% *}"'", texts: ["hello"], schema: {entities: {"person": "people"}, classifications: [{"name": "severity", "labels": ["Info", "Critical"], "cls_threshold": null}]}}')
cls_out=$(curl -s -o "$SMOKE_TMPDIR/onnx-smoke-cls.json" -w '%{http_code}' --max-time 10 \
	"$URL/v1/extract" -H 'Content-Type: application/json' -d "$cls_body")
cls_curl_rc=$?
cls_json=$(cat "$SMOKE_TMPDIR/onnx-smoke-cls.json" 2>/dev/null)
cls_code=$(echo "$cls_json" | jq -r '.error.code // empty' 2>/dev/null)
if [ "$cls_curl_rc" -ne 0 ]; then
	bad "malformed cls_threshold request failed at the transport level (curl rc=$cls_curl_rc) — this IS the old dropped-connection regression"
elif [ -z "$cls_json" ]; then
	bad "malformed cls_threshold request returned an EMPTY body (status reported: $cls_out) — this IS the old dropped-connection regression, just with a status code attached"
elif ! echo "$cls_out" | grep -qE '^4[0-9][0-9]$'; then
	bad "malformed cls_threshold request returned $cls_out (expected 4xx). Response: $cls_json"
elif [ "$cls_code" != "bad_schema" ]; then
	bad "malformed cls_threshold request returned $cls_out but error.code was '${cls_code:-absent}' (expected 'bad_schema'). Response: $cls_json"
else
	pass "malformed cls_threshold -> $cls_out error.code=bad_schema, with a real response body (no dropped connection)"
fi

# ── 7. Batch throughput (slow, opt-in) ──────────────────────────────────────────────────────
if [ "$SLOW" -eq 1 ]; then
	first_model="${MODELS%% *}"
	note "measuring throughput on a 32-text batch (model=$first_model) ..."
	batch_body=$(jq -n --arg m "$first_model" --arg t "$TEXT_ONE" --argjson s "$SCHEMA" \
		'{model: $m, texts: [range(32) | $t], schema: $s}')
	t0=$(date +%s%N)
	batch_json=$(curl -s --max-time 300 "$URL/v1/extract" -H 'Content-Type: application/json' -d "$batch_body")
	t1=$(date +%s%N)
	n=$(echo "$batch_json" | jq -r '(.results // []) | length')
	if [ "$n" = "32" ]; then
		ms=$(((t1 - t0) / 1000000))
		pass "32-text batch returned 32 results in ${ms}ms ($((ms / 32))ms/text)"
	else
		bad "32-text batch returned ${n:-no} results. Response head: $(echo "$batch_json" | head -c 300)"
	fi
else
	note "SKIPPED: 32-text throughput measurement (pass --slow to run it)"
fi

echo
if [ "$fail" -eq 0 ]; then
	echo "[smoke] all checks passed"
	exit 0
else
	echo "[smoke] one or more checks FAILED" >&2
	exit 1
fi
