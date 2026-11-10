#!/usr/bin/env bash
# router/smoke-router.sh — probe a router gateway instance (the resident :4813 Rust gateway, or a
# throwaway/shadow instance) by response body, never status code alone (repo quirk: "a healthy
# service has told you nothing" — llvmpipe-style, HTTP-200-but-wrong-answer failures are the
# family this guards against).
#
# Usage:
#   router/smoke-router.sh <base-url> [<gateway-key>] [--subscription] [--codex] [--chained]
#
#   <base-url>      mandatory, e.g. http://127.0.0.1:4813 (resident) or a transient
#                   http://127.0.0.1:470x (WP-1 scratch-decade run).
#   <gateway-key>   the x-gateway-key auth value for the local checks below. Falls back to
#                   $GATEWAY_KEY; otherwise the script re-execs under `fleet-env run router-client`,
#                   so neither the operator nor an agent handles the secret. Never echoed. Required for checks 1, 2 and 4 to pass (check 3
#                   deliberately omits/misconfigures it).
#   --subscription  default OFF, and this script NEVER passes it to itself. Without it, the
#                   hosted-leg check is skipped and only PRINTS what it would run — no request
#                   ever reaches api.anthropic.com. Passing it runs one `claude -p` call through
#                   this gateway on the operator's saved claude.ai login, pinned to the ANTHROPIC_CHEAP slot (test-slots.json; one tiny
#                   subscription turn). Standard since 2026-10-30: agents pass it without asking
#                   (router/AGENTS.md "Conditional gates"; origin docs/tool-calling-gateway/oauth-passthrough-probe.md).
#   Slots: test-slots.json (TEST_SLOT_<NAME> overrides one for one run); every driven turn is on a slot and
#   fails loudly when the slot's routed id is not listed.
#   Env: CODEX_TEST_PERSONA (persona for the --codex turn; default: first codex-* provider /v1/models lists),
#   CODEX_TEST_PERSONAS (space separated; the personas whose model ids check 1 requires; default: every codex-* catalog provider),
#   SMOKE_CATALOG_FILE (catalog check 1 compares against; default the repo catalog).
#
#   --chained       chained-node mode (a client node whose claude-local/* and codex-*/* providers
#                   use the upstream-gateway credential and relay to the tower's router, e.g.
#                   a laptop relaying to the main host). Every Anthropic-ingress check runs unchanged (through the chain);
#                   check 4 instead asserts that /v1/chat/completions on claude-local/* is an
#                   intentional 400 protocol refusal (the chained provider speaks only the
#                   Anthropic protocol). Default (tower) mode is unchanged.
#
# Checks (all by response body):
#   1. GET /v1/models returns a non-empty id list that equals, as a set, the ids router/catalog/catalog.json
#      declares (<provider>/<id> for every entry of every provider; computed at run time, no ids named
#      here); on mismatch both difference directions are printed. Every codex persona under test has
#      at least one listed id.
#   2. POST /v1/messages via claude-local/<LOCAL_SMALL slot> with thinking:{"type":"disabled"}
#      returns 2xx and NO thinking content block — the deterministic half of the router's
#      effort mapping (the slot's effortKwargs.disabled row in catalog/catalog.json, checked first).
#      The low-vs-medium token delta is prompt- and slot-state-dependent, so no gate asserts it.
#   3. A request with a missing gateway key, and one with a wrong gateway key, each return a
#      401 error body (asserted on the body's error message, not just the status).
#   4. One non-streamed chat completion via claude-local/<LOCAL_SMALL slot> returns non-empty content
#      (--chained: returns a 400 protocol-refusal error body instead).
#   5. POST /v1/messages/count_tokens via claude-local/<LOCAL_SMALL slot> returns an integer
#      input_tokens field (local leg; patch 0004).
#   6. POST /v1/messages/count_tokens with an unresolvable model returns a 404-class JSON
#      error body (patch 0004).
#   7. (--subscription only) one claude -p call on the ANTHROPIC_CHEAP slot through the gateway returns a
#      completion (anthropic/<slot> must be listed first).
#   8. (--codex only: one turn on the operator's Codex subscription, <codex persona>/<CODEX_SIDECAR_CHEAP slot>
#      from test-slots.json) one non-streamed completion via /v1/messages returns
#      usage.output_tokens > 0 — proves the host sidecar leg (plans/codex-appserver-sidecar.md)
#      end to end through the router: extra_hosts, the shared CODEX_SIDECAR_KEY, and the
#      engine's baseurl-with-path derivation. (Not input_tokens: since 2026-09-13 the sidecar
#      emits Anthropic usage semantics, so a fully cached turn legitimately reports
#      input_tokens 0 with the prompt in cache_read_input_tokens.)
#   9. POST /v1/messages/count_tokens via the same codex id as check 8 returns an integer
#      input_tokens (unmetered — the sidecar answers locally with a chars/4 estimate).
#  10. (SMOKE_SYSTEMONE=1 only, metered: one OpenRouter decision call) POST /v1/systemone via
#      openrouter-decide/<first catalog entry of that provider> with a synthetic state returns
#      answers.wait.choice in {blocked, offer, none} (patch 0013). Default off.
#  11. POST /v1/messages via openrouter/<first catalog entry of that provider> is a 4xx protocol-mismatch refusal
#      (plans/router-rust-gateway.md Decision 2: an OpenAI-protocol provider is never reached
#      from Anthropic ingress; no upstream call, unbilled).
#
# Exits non-zero on any check failure. Requires: curl, jq.
# >>> shguard modes=exec (generated by shell-disposition-gate --guard; do not edit)
case $(realpath -- "$0" 2>/dev/null) in */'router/smoke-router.sh') _shguard_m='exec' ;; *) if [ "${SHGUARD_MODE-}" = 'pipe' ]; then _shguard_m='pipe'; else _shguard_m='source'; fi ;; esac
# shellcheck disable=SC2317
case $_shguard_m in
  exec) unset _shguard_m ;;
  *) printf '%s: refusing %s invocation (supports: exec; pipe callers set SHGUARD_MODE=pipe)\n' 'smoke-router.sh' "$_shguard_m" >&2; unset _shguard_m; return 1 2>/dev/null || exit 1 ;;
esac
# <<< shguard
for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '<base-url> [<gateway-key>] [--subscription] [--codex] [--chained]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../scripts/lib/build-id.sh"
# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../scripts/lib/test-slots.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

set -u

SUBSCRIPTION=0
CODEX=0
CHAINED=0
BASE_URL=""
GATEWAY_KEY="${GATEWAY_KEY:-}"

for arg in "$@"; do
	case "$arg" in
	--subscription)
		SUBSCRIPTION=1
		;;
	--codex)
		CODEX=1
		;;
	--chained)
		CHAINED=1
		;;
	*)
		if [ -z "$BASE_URL" ]; then
			BASE_URL="$arg"
		elif [ -z "$GATEWAY_KEY" ]; then
			GATEWAY_KEY="$arg"
		fi
		;;
	esac
done

if [ -z "$BASE_URL" ]; then
	echo "usage: $0 <base-url> [<gateway-key>] [--subscription] [--codex] [--chained]" >&2
	exit 2
fi

BASE_URL="${BASE_URL%/}"

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

# Key: the <gateway-key> arg or $GATEWAY_KEY win; otherwise re-exec under the fleet-env
# router-client consumer, which puts only GATEWAY_KEY in the environment — this script never
# opens a projection or other credential file, and never echoes the key.
if [ -z "${GATEWAY_KEY:-}" ] && [ "${FLEET_ENV_RUN:-}" != router-client ]; then
	FLEET_ENV=$(command -v "${FLEET_ENV_BIN:-fleet-env}" 2>/dev/null) || FLEET_ENV=""
	if [ ! -f "$FLEET_ENV" ]; then
		echo "FATAL: fleet-env binary ${FLEET_ENV_BIN:-fleet-env} not found (set FLEET_ENV_BIN or put fleet-env on PATH); refusing to run without credentials" >&2
		exit 2
	fi
	exec "$FLEET_ENV" run router-client -- bash "${BASH_SOURCE[0]}" "$@"
fi
: "${GATEWAY_KEY:?no gateway key: pass <gateway-key>, set GATEWAY_KEY, or run under fleet-env run router-client}"

# ── 1. GET /v1/models lists exactly what the catalog declares ───────────────────────────────
# No ids are named here: the router lists, per provider, every catalog entry as <provider>/<id>
# (verified against the live router 2026-11-10: the listing equals the naive set, no hidden
# entries, wildcards or preset rewriting). The expected set is computed from the catalog at run
# time ($SMOKE_CATALOG_FILE, default the repo catalog), so a catalog edit never needs a test edit.
SMOKE_CATALOG_FILE="${SMOKE_CATALOG_FILE:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/catalog/catalog.json}"

# models_diff <catalog.json> <listed ids, newline separated>: prints "extra:" (listed, not declared)
# and "missing:" (declared, not listed) lines, each direction capped to 10 ids; prints nothing
# when the sets are equal. Pure: reads only its two inputs.
models_diff() {
	local declared extra missing
	declared=$(jq -r '.providers | to_entries[] | .key as $p | .value.models[]? | "\($p)/\(.id)"' "$1" | sort -u) || return 2
	extra=$(comm -13 <(printf '%s\n' "$declared") <(printf '%s\n' "$2" | sort -u))
	missing=$(comm -23 <(printf '%s\n' "$declared") <(printf '%s\n' "$2" | sort -u))
	[ -z "$extra" ] || printf 'listed-not-declared (%s): %s\n' "$(printf '%s\n' "$extra" | wc -l)" "$(printf '%s\n' "$extra" | head -n 10 | tr '\n' ' ')"
	[ -z "$missing" ] || printf 'declared-not-listed (%s): %s\n' "$(printf '%s\n' "$missing" | wc -l)" "$(printf '%s\n' "$missing" | head -n 10 | tr '\n' ' ')"
	return 0
}

# codex_missing_personas <listed ids> <personas, space separated, or empty>: prints the personas with
# no listed id (default personas: every codex-* provider in the catalog).
codex_missing_personas() {
	local persona
	for persona in $2; do
		printf '%s\n' "$1" | grep -q "^${persona}/" || printf '%s ' "$persona"
	done
}

note "checking GET /v1/models ..."
models_json=$(curl -s --max-time 10 -H "x-gateway-key: $GATEWAY_KEY" "$BASE_URL/v1/models") || models_json=""
model_ids=$(echo "$models_json" | jq -r '.data[]?.id // empty' 2>/dev/null)
if [ ! -f "$SMOKE_CATALOG_FILE" ]; then
	bad "/v1/models: catalog $SMOKE_CATALOG_FILE not found"
elif [ -z "$model_ids" ]; then
	bad "/v1/models returned no ids (not well-formed JSON with a non-empty .data[].id list). Response: $(echo "$models_json" | head -c 300)"
else
	models_delta=$(models_diff "$SMOKE_CATALOG_FILE" "$model_ids")
	# Personas under test: $CODEX_TEST_PERSONAS (space separated), else every codex-* provider in the catalog.
	codex_personas="${CODEX_TEST_PERSONAS:-$(jq -r '.providers | keys[] | select(startswith("codex-"))' "$SMOKE_CATALOG_FILE" | tr '\n' ' ')}"
	codex_absent=$(codex_missing_personas "$model_ids" "$codex_personas")
	if [ -n "$models_delta" ]; then
		bad "/v1/models differs from the catalog ($SMOKE_CATALOG_FILE): $(printf '%s' "$models_delta" | tr '\n' ';')"
	elif [ -z "${codex_personas// /}" ]; then
		bad "/v1/models: no codex persona under test (set CODEX_TEST_PERSONAS or declare a codex-* provider in the catalog)"
	elif [ -n "${codex_absent// /}" ]; then
		bad "/v1/models lists no ids for codex persona(s): $codex_absent"
	else
		pass "/v1/models lists exactly the $(printf '%s\n' "$model_ids" | wc -l) catalog-declared ids; every codex persona under test ($codex_personas) has listed ids"
	fi
fi

# Driven local turns (checks 2, 4, 5) run on claude-local/<LOCAL_SMALL slot>, resolved exactly: an
# unlisted routed id fails the leg, never falls back to another model.
local_small=$(test_slot LOCAL_SMALL) || exit 2
LOCAL_MODEL="claude-local/$local_small"
local_served=1
if ! printf '%s\n' "$model_ids" | grep -qxF -- "$LOCAL_MODEL"; then
	local_served=0
	slot_not_served LOCAL_SMALL "$local_small"
fi

# ── 2. Effort mapping, deterministic half: thinking disabled ⇒ no thinking block ────────────
# Thinking-disabled mapping: the LOCAL_SMALL slot's `effortKwargs.disabled` row in catalog/catalog.json turns
# thinking:{"type":"disabled"} into the upstream body's reasoning-off kwarg (measured 2026-10-19 on
# nemotron-4b: enable_thinking:false gives reasoning length 0 versus 71). That is the one level
# whose effect is deterministic — the low-vs-medium token delta is prompt-dependent and
# slot-state-dependent, so it is not gated anywhere. First request after a model switch may
# include a llama-swap load, hence the long timeout.
if [ "$local_served" -eq 0 ]; then
	bad "/v1/messages ($LOCAL_MODEL, thinking disabled): the routed id is not listed in /v1/models"
elif ! jq -e --arg id "$local_small" '[.providers["claude-local"].models[] | select(.id == $id) | .effortKwargs.disabled] | any(. != null)' "$SMOKE_CATALOG_FILE" >/dev/null 2>&1; then
	bad "/v1/messages ($LOCAL_MODEL, thinking disabled): slot LOCAL_SMALL=$local_small has no effortKwargs.disabled row in $SMOKE_CATALOG_FILE — add the row or edit test-slots.json (no turn spent)"
else
note "checking POST /v1/messages (model=$LOCAL_MODEL, thinking disabled) ..."
effort_status=$(curl -s --max-time 300 -o /tmp/smoke-router-effort-body.$$ -w '%{http_code}' \
	-X POST "$BASE_URL/v1/messages" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-H 'anthropic-version: 2023-06-01' \
	-d "$(jq -nc --arg m "$LOCAL_MODEL" '{model:$m,max_tokens:128,stream:false,thinking:{type:"disabled"},messages:[{role:"user",content:"Reply with the single word: pong"}]}')") || effort_status=""
effort_json=$(cat /tmp/smoke-router-effort-body.$$ 2>/dev/null)
rm -f /tmp/smoke-router-effort-body.$$
effort_types=$(echo "$effort_json" | jq -r '[.content[]?.type] | join(",")' 2>/dev/null)
if [ "${effort_status#2}" != "$effort_status" ] && [ -n "$effort_types" ] && ! echo "$effort_types" | grep -q thinking; then
	pass "/v1/messages ($LOCAL_MODEL, thinking disabled) returned $effort_status with content blocks [$effort_types] — no thinking block"
else
	bad "/v1/messages ($LOCAL_MODEL, thinking disabled) expected 2xx with no thinking block (status=$effort_status, blocks=[$effort_types]). Response: $(echo "$effort_json" | head -c 300)"
fi
fi

# ── 3. Missing / wrong gateway key both 401 by body ─────────────────────────────────────────
note "checking GET /v1/models with a missing gateway key ..."
missing_json=$(curl -s --max-time 10 "$BASE_URL/v1/models") || missing_json=""
missing_msg=$(echo "$missing_json" | jq -r '.error.message // empty' 2>/dev/null)
if [ -n "$missing_msg" ]; then
	pass "missing key rejected with body error: $missing_msg"
else
	bad "missing key did not produce a body error message. Response: $missing_json"
fi

note "checking GET /v1/models with a wrong gateway key ..."
wrong_json=$(curl -s --max-time 10 -H "x-gateway-key: wp1-smoke-wrong-key" "$BASE_URL/v1/models") || wrong_json=""
wrong_msg=$(echo "$wrong_json" | jq -r '.error.message // empty' 2>/dev/null)
if [ -n "$wrong_msg" ]; then
	pass "wrong key rejected with body error: $wrong_msg"
else
	bad "wrong key did not produce a body error message. Response: $wrong_json"
fi

# ── 4. Local chat completion via claude-local/<LOCAL_SMALL slot> ──────────────────────────────────────────────────────────────
if [ "$local_served" -eq 0 ]; then
	bad "/v1/chat/completions ($LOCAL_MODEL): the routed id is not listed in /v1/models"
elif [ "$CHAINED" -eq 1 ]; then
	note "--chained: checking POST /v1/chat/completions (model=$LOCAL_MODEL) is a 400 protocol refusal ..."
	chain_out=$(curl -s --max-time 15 -w '\n%{http_code}' -X POST "$BASE_URL/v1/chat/completions" \
		-H 'Content-Type: application/json' \
		-H "x-gateway-key: $GATEWAY_KEY" \
		-d "$(jq -nc --arg m "$LOCAL_MODEL" '{model:$m,max_tokens:1,stream:false,messages:[{role:"user",content:"ping"}]}')") || chain_out=""
	chain_status=$(printf '%s' "$chain_out" | tail -n 1)
	chain_body=$(printf '%s' "$chain_out" | sed '$d')
	chain_msg=$(echo "$chain_body" | jq -r '.error.message // empty' 2>/dev/null)
	if [ "$chain_status" = "400" ] && [ -n "$chain_msg" ]; then
		pass "--chained: /v1/chat/completions ($LOCAL_MODEL) refused with 400: $chain_msg"
	else
		bad "--chained: /v1/chat/completions ($LOCAL_MODEL) expected a 400 refusal body (status=$chain_status). Response: $chain_body"
	fi
else
note "checking POST /v1/chat/completions (model=$LOCAL_MODEL) ..."
chat_json=$(curl -s --max-time 120 -X POST "$BASE_URL/v1/chat/completions" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d "$(jq -nc --arg m "$LOCAL_MODEL" '{model:$m,max_tokens:256,stream:false,messages:[{role:"user",content:"Reply with the single word: pong"}]}')") || chat_json=""
chat_content=$(echo "$chat_json" | jq -r '
	(.choices[0].message.content // empty)
	| if type == "string" and length > 0 then . else empty end
' 2>/dev/null)
if [ -n "$chat_content" ]; then
	pass "/v1/chat/completions ($LOCAL_MODEL) returned non-empty content"
else
	bad "/v1/chat/completions ($LOCAL_MODEL) did not return non-empty content. Response: $chat_json"
fi
fi

# ── 5. Local-leg count_tokens (claude-local/<LOCAL_SMALL slot>) ──────────────────────────────────────────────────────────────────
if [ "$local_served" -eq 0 ]; then
	bad "/v1/messages/count_tokens ($LOCAL_MODEL): the routed id is not listed in /v1/models"
else
note "checking POST /v1/messages/count_tokens (model=$LOCAL_MODEL) ..."
count_tokens_json=$(curl -s --max-time 30 -X POST "$BASE_URL/v1/messages/count_tokens" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d "$(jq -nc --arg m "$LOCAL_MODEL" '{model:$m,messages:[{role:"user",content:"Reply with the single word: pong"}]}')") || count_tokens_json=""
count_tokens_value=$(echo "$count_tokens_json" | jq -r '
	.input_tokens
	| if type == "number" and (. == (. | floor)) then . else empty end
' 2>/dev/null)
if [ -n "$count_tokens_value" ]; then
	pass "/v1/messages/count_tokens ($LOCAL_MODEL) returned integer input_tokens=$count_tokens_value"
else
	bad "/v1/messages/count_tokens ($LOCAL_MODEL) did not return an integer input_tokens. Response: $count_tokens_json"
fi
fi

# ── 6. count_tokens with an unresolvable model returns a 404-class JSON error body ──────────
note "checking POST /v1/messages/count_tokens with an unresolvable model ..."
wrong_model_status=$(curl -s --max-time 15 -o /tmp/smoke-router-wrong-model-body.$$ -w '%{http_code}' \
	-X POST "$BASE_URL/v1/messages/count_tokens" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d '{"model":"wp2-smoke-nonexistent-model","messages":[{"role":"user","content":"hi"}]}') || wrong_model_status=""
wrong_model_json=$(cat /tmp/smoke-router-wrong-model-body.$$ 2>/dev/null)
rm -f /tmp/smoke-router-wrong-model-body.$$
wrong_model_msg=$(echo "$wrong_model_json" | jq -r '.error.message // empty' 2>/dev/null)
if [ "$wrong_model_status" = "404" ] && [ -n "$wrong_model_msg" ]; then
	pass "/v1/messages/count_tokens with an unresolvable model returned 404 with a body error: $wrong_model_msg"
else
	bad "/v1/messages/count_tokens with an unresolvable model did not return a 404 body error (status=$wrong_model_status). Response: $wrong_model_json"
fi

# ── 7. Hosted-leg subscription probe (opt-in flag; standard, on the ANTHROPIC_CHEAP slot) ───────────────────────────
anthropic_slot=$(test_slot ANTHROPIC_CHEAP) || exit 2
ANTHROPIC_MODEL="anthropic/$anthropic_slot"
if [ "$SUBSCRIPTION" -eq 1 ] && ! printf '%s\n' "$model_ids" | grep -qxF -- "$ANTHROPIC_MODEL"; then
	slot_not_served ANTHROPIC_CHEAP "$anthropic_slot"
	bad "--subscription: claude -p ($ANTHROPIC_MODEL): the routed id is not listed in /v1/models"
elif [ "$SUBSCRIPTION" -eq 1 ]; then
	note "--subscription passed: running one claude -p call through $BASE_URL on the saved claude.ai login."
	note "This bills the operator's real Claude subscription."
	if ! command -v claude >/dev/null 2>&1; then
		bad "--subscription: 'claude' CLI not found on PATH"
	else
		sub_out=$(ANTHROPIC_BASE_URL="$BASE_URL" ANTHROPIC_CUSTOM_HEADERS="x-gateway-key: $GATEWAY_KEY" claude -p --model "$anthropic_slot" "Reply with the single word: pong" 2>&1)
		sub_rc=$?
		if [ "$sub_rc" -eq 0 ] && [ -n "$sub_out" ]; then
			pass "--subscription: claude -p returned a non-empty completion through the hosted leg"
		else
			bad "--subscription: claude -p failed (rc=$sub_rc). Output: $sub_out"
		fi
	fi
else
	note "--subscription not passed: SKIPPING the hosted-leg claude -p probe (one ANTHROPIC_CHEAP slot turn on the"
	note "operator's Claude subscription). What it would run:"
	note "  ANTHROPIC_BASE_URL=$BASE_URL ANTHROPIC_CUSTOM_HEADERS='x-gateway-key: <key>' claude -p --model $anthropic_slot 'Reply with the single word: pong'"
fi

# ── 8. Codex sidecar leg, metered (--codex only) ────────────────────────────────────────────
# The routed id is <codex persona>/<CODEX_SIDECAR_CHEAP slot> (test-slots.json; $TEST_SLOT_CODEX_SIDECAR_CHEAP overrides
# it for one run) — the only end-to-end proof a newly added codex id is accepted, since the sidecar's
# count_tokens never checks the id.
codex_test=$(test_slot CODEX_SIDECAR_CHEAP) || exit 2
codex_persona=$(codex_test_persona "$model_ids") || exit 2
CODEX_MODEL="$codex_persona/$codex_test"
if [ "$CODEX" -eq 1 ] && ! printf '%s\n' "$model_ids" | grep -qxF -- "$CODEX_MODEL"; then
	slot_not_served CODEX_SIDECAR_CHEAP "$codex_test"
	bad "/v1/messages ($CODEX_MODEL): the routed id is not listed in /v1/models"
elif [ "$CODEX" -eq 1 ]; then
	note "--codex passed: running one non-streamed $CODEX_MODEL turn through $BASE_URL (bills the operator's Codex subscription)."
	# The engine's anthropic-type leg needs *some* client bearer to build passthrough headers
	# (openrouter-spike finding 3); the provider's apiKeyEnv replaces it before the sidecar.
	codex_json=$(curl -s --max-time 180 -X POST "$BASE_URL/v1/messages" \
		-H 'Content-Type: application/json' \
		-H "x-gateway-key: $GATEWAY_KEY" \
		-H 'authorization: Bearer smoke-dummy-client-bearer' \
		-H 'anthropic-version: 2023-06-01' \
		-d "$(jq -nc --arg m "$CODEX_MODEL" '{model:$m,max_tokens:32,stream:false,metadata:{user_id:"{\"session_id\":\"smoke-router-codex\"}"},messages:[{role:"user",content:"Reply with the single word: pong"}]}')") || codex_json=""
	# Anthropic usage semantics (2026-09-13): input_tokens is the *uncached* prompt share, so a
	# fully cached turn legitimately reports 0 — output_tokens is the liveness signal here.
	codex_out=$(echo "$codex_json" | jq -r '.usage.output_tokens | if type == "number" and . > 0 then . else empty end' 2>/dev/null)
	if [ -n "$codex_out" ]; then
		pass "/v1/messages ($CODEX_MODEL) returned usage.output_tokens=$codex_out"
	else
		bad "/v1/messages ($CODEX_MODEL) did not return usage.output_tokens > 0. Response: $codex_json"
	fi
else
	note "--codex not passed: SKIPPING the metered $CODEX_MODEL probe (one Codex-subscription turn)."
fi

# ── 9. Codex sidecar count_tokens (unmetered) ───────────────────────────────────────────────
note "checking POST /v1/messages/count_tokens (model=$CODEX_MODEL) ..."
codex_ct_json=$(curl -s --max-time 30 -X POST "$BASE_URL/v1/messages/count_tokens" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-H 'authorization: Bearer smoke-dummy-client-bearer' \
	-d "$(jq -nc --arg m "$CODEX_MODEL" '{model:$m,messages:[{role:"user",content:"Reply with the single word: pong"}]}')") || codex_ct_json=""
codex_ct_value=$(echo "$codex_ct_json" | jq -r '
	.input_tokens
	| if type == "number" and (. == (. | floor)) then . else empty end
' 2>/dev/null)
if [ -n "$codex_ct_value" ]; then
	pass "/v1/messages/count_tokens ($CODEX_MODEL) returned integer input_tokens=$codex_ct_value"
else
	bad "/v1/messages/count_tokens ($CODEX_MODEL) did not return an integer input_tokens. Response: $codex_ct_json"
fi

# first_catalog_id <provider>: the first catalog entry of that provider (these legs probe the provider, not a model
# choice); non-zero with a message naming the provider when it has none.
first_catalog_id() {
	local id
	id=$(jq -r --arg p "$1" '.providers[$p].models[0].id // empty' "$SMOKE_CATALOG_FILE" 2>/dev/null)
	if [ -z "$id" ]; then
		echo "provider $1 has no entries in $SMOKE_CATALOG_FILE" >&2
		return 1
	fi
	printf '%s\n' "$id"
}

# ── 10. systemone decision route, metered (SMOKE_SYSTEMONE=1 only) ─────────────────────────
if [ "${SMOKE_SYSTEMONE:-0}" = "1" ] && ! decide_id=$(first_catalog_id openrouter-decide); then
	bad "/v1/systemone: openrouter-decide provider has no catalog entry to probe"
elif [ "${SMOKE_SYSTEMONE:-0}" = "1" ]; then
	DECIDE_MODEL="openrouter-decide/$decide_id"
	note "SMOKE_SYSTEMONE=1: running one /v1/systemone call ($DECIDE_MODEL; bills OpenRouter)."
	so_json=$(curl -s --max-time 60 -X POST "$BASE_URL/v1/systemone" \
		-H 'Content-Type: application/json' \
		-H "x-gateway-key: $GATEWAY_KEY" \
		-d "$(jq -nc --arg m "$DECIDE_MODEL" '{model:$m,state:"Assistant final message: I have finished the refactor. Should I also delete the old module, yes or no?",questions:{wait:{type:"choice",instructions:"Is the assistant blocked waiting on the user, offering optional follow-up, or not waiting?",criteria:{blocked:"The assistant needs an answer or decision before it can continue.",offer:"The assistant offers optional further work but is done.",none:"The assistant is not waiting on the user."}}}}')") || so_json=""
	so_choice=$(echo "$so_json" | jq -r '.answers.wait.choice | select(. == "blocked" or . == "offer" or . == "none")' 2>/dev/null)
	if [ -n "$so_choice" ]; then
		pass "/v1/systemone ($DECIDE_MODEL) returned answers.wait.choice=$so_choice"
	else
		bad "/v1/systemone ($DECIDE_MODEL) did not return answers.wait.choice in {blocked,offer,none}. Response: $so_json"
	fi
else
	note "SMOKE_SYSTEMONE not set: SKIPPING the metered /v1/systemone probe (one OpenRouter decision call)."
fi

# ── 11. Decision 2: openrouter/* on Anthropic ingress is refused, never relayed ─────────────
if ! or_id=$(first_catalog_id openrouter); then
	bad "/v1/messages (openrouter/*): the openrouter provider has no catalog entry to probe"
else
OR_MODEL="openrouter/$or_id"
note "checking POST /v1/messages via $OR_MODEL is a 4xx refusal ..."
or_out=$(curl -s --max-time 15 -w '\n%{http_code}' -X POST "$BASE_URL/v1/messages" \
	-H 'Content-Type: application/json' \
	-H "x-gateway-key: $GATEWAY_KEY" \
	-d "$(jq -nc --arg m "$OR_MODEL" '{model:$m,max_tokens:1,messages:[{role:"user",content:"ping"}]}')") || or_out=""
or_status=$(printf '%s' "$or_out" | tail -n 1)
or_body=$(printf '%s' "$or_out" | sed '$d')
or_has_content=$(echo "$or_body" | jq -r 'if (.content | type) == "array" then "yes" else empty end' 2>/dev/null)
if [ "${or_status:0:1}" = "4" ] && [ -z "$or_has_content" ]; then
	pass "/v1/messages ($OR_MODEL) refused with $or_status and no assistant content"
else
	bad "/v1/messages ($OR_MODEL) expected a 4xx refusal, got status '$or_status'. Response: $or_body"
fi
fi

echo
if [ "$fail" -eq 0 ]; then
	echo "[smoke] all checks passed"
	exit 0
else
	echo "[smoke] one or more checks FAILED" >&2
	exit 1
fi
