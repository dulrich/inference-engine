#!/usr/bin/env bash
# Host-run smoke test for inference-engine-onnx's POST /v1/classify (seqcls models).
#
# Sibling of smoke-extract.sh, kept separate so the GLiNER2 extract checks and their default
# model list stay untouched. Run AFTER the container is up; it touches no container.
#
# Usage:
#   onnx/smoke-classify.sh [--url http://127.0.0.1:4812] [--model hsc-turn-state] [--extract-model gliner2-base]
#
# Same discipline as smoke-extract.sh: assertions are on the RESPONSE BODY and on SHAPE, never
# on values (label is one of the model's own labels, probabilities sum to ~1, one result per
# text). A status code is checked only where the code is the contract (wrong-type 400).
#
# Exits non-zero on any check failure. Requires: curl, jq.
for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '[--url <url>] [--model <alias>] [--extract-model <alias>]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

set -u

URL="http://127.0.0.1:4812"
MODEL="hsc-turn-state"
EXTRACT_MODEL="gliner2-base"

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
	--extract-model)
		EXTRACT_MODEL="$2"
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

# ── 1. /health: the seqcls model is resident and reports its manifest ───────────────────────
note "checking GET /health for $MODEL ..."
health_json=$(curl -s --max-time 15 "$URL/health") || health_json=""
info=$(echo "$health_json" | jq -c --arg m "$MODEL" '.models[$m] // empty' 2>/dev/null)
if echo "$info" | jq -e '.loaded == true and .type == "seqcls" and (.labels | type == "array" and length > 0) and (.max_len | type == "number") and (.format | type == "string")' >/dev/null 2>&1; then
	pass "$MODEL loaded, type=seqcls, format=$(echo "$info" | jq -r .format), labels=$(echo "$info" | jq -c .labels)"
else
	bad "$MODEL not loaded as a seqcls model in /health. Entry: ${info:-absent}"
fi
labels=$(echo "$info" | jq -c '.labels // []' 2>/dev/null)
[ -n "$labels" ] || labels='[]'

# ── 2. POST /v1/classify: one well-formed result per text ───────────────────────────────────
note "checking POST /v1/classify (3 texts) ..."
body=$(jq -n --arg m "$MODEL" '{model: $m, texts: [
	"Done. All tests pass and the change is committed.",
	"Should I proceed with option A or option B?",
	"Running the full test suite now; this will take a few minutes."]}')
resp=$(curl -s --max-time 30 "$URL/v1/classify" -H 'Content-Type: application/json' -d "$body")
if echo "$resp" | jq -e --arg m "$MODEL" --argjson labels "$labels" '
	.model == $m and (.results | type == "array" and length == 3) and all(.results[];
		(.label as $l | $labels | index($l)) != null
		and (.confidence | type == "number")
		and (.probabilities | type == "object" and (keys | sort) == ($labels | sort))
		and ([.probabilities[]] | add | . > 0.999 and . < 1.001)
		and .confidence == .probabilities[.label])' >/dev/null 2>&1; then
	pass "3 results, labels $(echo "$resp" | jq -c '[.results[].label]'), probabilities sum ~1"
else
	bad "classify response has the wrong shape. Response: $(echo "$resp" | head -c 600)"
fi

# ── 3/4. NEGATIVE: the wrong model type is a 400 naming the type, on both routes ────────────
check_wrong_type() {
	local route="$1" req="$2" want_type="$3"
	local code json
	code=$(curl -s -o "$tmp" -w '%{http_code}' --max-time 15 "$URL$route" \
		-H 'Content-Type: application/json' -d "$req")
	json=$(cat "$tmp" 2>/dev/null)
	if [ "$code" = "400" ] && echo "$json" | jq -e --arg t "'$want_type'" \
		'.error.code == "wrong_model_type" and (.error.message | contains($t))' >/dev/null 2>&1; then
		pass "$route with a $want_type model -> 400 wrong_model_type"
	else
		bad "$route with a $want_type model returned $code. Response: $json"
	fi
}
tmp=$(mktemp) || {
	echo "[smoke] FATAL: mktemp failed." >&2
	exit 2
}
trap 'rm -f "$tmp"' EXIT
check_wrong_type /v1/extract "$(jq -n --arg m "$MODEL" '{model: $m, texts: ["hello"], schema: {entities: ["person"]}}')" seqcls
check_wrong_type /v1/classify "$(jq -n --arg m "$EXTRACT_MODEL" '{model: $m, texts: ["hello"]}')" gliner2

echo
if [ "$fail" -eq 0 ]; then
	echo "[smoke] all checks passed"
	exit 0
else
	echo "[smoke] one or more checks FAILED" >&2
	exit 1
fi
