#!/usr/bin/env bash
# router/smoke-tool-search.sh — end-to-end deferred-tool (ENABLE_TOOL_SEARCH) conformance for the
# resident router, asserted on response/request BODIES, never status alone (repo quirk: "a healthy
# service has told you nothing"). Companion to smoke-router.sh, same conventions.
#
# It locks the four invariants the WP-5 wire spike named
# (docs/tool-calling-gateway/tool-search-conformance-spike.md, "What WP-6's smoke-tool-search.sh
# must assert") by driving a real `claude -p` turn per leg in a throwaway profile and inspecting
# what Claude Code actually put on the wire.
#
# Usage:
#   router/smoke-tool-search.sh <base-url> [<gateway-key>] [--local] [--codex] [--passthrough]
#                               [--control] [--keep]
#
#   <base-url>      mandatory, e.g. http://127.0.0.1:4813.
#   <gateway-key>   x-gateway-key auth value; falls back to $GATEWAY_KEY; otherwise the script
#                   re-execs under `fleet-env run router-client` (only GATEWAY_KEY),
#                   so the operator never has to
#                   put the secret on a command line. The key is never echoed.
#   --local         the claude-local/* OpenAI-conversion leg. Unmetered (local GPU). DEFAULT
#                   when no leg flag is given.
#   --codex         the codex sidecar leg. METERED: one turn on the operator's Codex subscription.
#   --passthrough   the hosted Claude leg. METERED: one turn on the operator's Claude subscription.
#                   Needs the saved OAuth login; the fixture gets a READ-ONLY SYMLINK to
#                   ~/.claude/.credentials.json — the credential is never copied and never read
#                   by this script.
#   --control       negative control: run the same checks with ENABLE_TOOL_SEARCH=false, where the
#                   catalogue is eagerly materialised. The conformance checks MUST fail and this
#                   script MUST exit non-zero. A conformance test that cannot fail is not a gate.
#   --keep          leave the fixture dirs and captures in place for inspection.
#
# Checks, per selected leg (all asserted on bodies):
#   1. Deferred references present in request 1 — `tools` carries exactly one `defer_loading:true`
#      entry (DeferredToolPlaceholder), a ToolSearch entry with a non-empty input_schema, and does
#      NOT carry the probe tool. Inverted under --control.
#   2. Discovery call made — the assistant emitted a `tool_use` block named ToolSearch.
#   3. References relayed — a `tool_result` carries {"type":"tool_reference","tool_name":...}
#      unchanged, and that request's `tools` array has grown by exactly one entry which has both a
#      full input_schema and `defer_loading:true` retained.
#   4. Concrete tool materialised — the probe tool's own parameter names reach the final answer,
#      which is only possible once its deferred schema was loaded.
#   5. Within-leg token gate — request 1's `system`+`tools` count_tokens is at or below the
#      measured on-threshold for THIS leg (plus 10% of drift: the throwaway profile's own paths and date move the system prompt by a few tokens, and each Claude Code release grows its own system prompt and built-in tool descriptions, ~+100 tokens 2026-10-19 → 2026-10-31), and above that same threshold
#      under --control. Thresholds were measured with the pinned tool set (PINNED_TOOLS): local (claude-local/nemotron-4b) 3983 on / 7351 control and codex (codex-b/gpt-6-luna) 3529 on, both 2026-10-31 on Claude Code 2.1.314 (request 1 carries 6 tools, one deferred; local splits 1275 system / 2708 tools); codex control 6380 and passthrough 11465 on / 15111 control are the 2026-10-19 figures (codex then on gpt-5.6-luna, whose count_tokens is identical). The margins are ~46% (local), ~45% (codex) and ~32% (passthrough), so
#      10% cannot mask an eager expansion. Cross-leg comparison is invalid (different catalogues, different tokenizers) and
#      is never attempted.
#
# How it observes request 1: Claude Code is pointed at a throwaway loopback capture proxy that
# logs each request body and forwards to <base-url> byte-for-byte. The proxy also replays
# count_tokens for check 5 in-process, using the live request's own Authorization header, so the
# hosted leg can be measured without any credential reaching disk or this script. It is written
# into a temp dir at run time, bound on an ephemeral port, and torn down with the fixture.
#
# The fixture never touches the real profile: a fresh $HOME, a fresh working dir, trust seeded for
# both the working dir and its parent, and deny rules over the real trees. Exits non-zero on any
# check failure. Requires: curl, jq, bun, claude.
for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '<base-url> [<gateway-key>] [--local] [--codex] [--passthrough] [--control] [--keep]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../scripts/lib/build-id.sh"
# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/test/test-models.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

set -u

PROBE_TOOL="NotebookEdit"
PROBE_PARAMS_RE='notebook_path|new_source|cell_id|cell_type'
PROMPT="What are the exact parameter names of the ${PROBE_TOOL} tool? Answer with the parameter names only."

BASE_URL=""
GATEWAY_KEY="${GATEWAY_KEY:-}"
CONTROL=0
KEEP=0
WANT_LOCAL=0
WANT_CODEX=0
WANT_PASSTHROUGH=0

for arg in "$@"; do
	case "$arg" in
	--local) WANT_LOCAL=1 ;;
	--codex) WANT_CODEX=1 ;;
	--passthrough) WANT_PASSTHROUGH=1 ;;
	--control) CONTROL=1 ;;
	--keep) KEEP=1 ;;
	--*)
		echo "usage: $0 <base-url> [<gateway-key>] [--local] [--codex] [--passthrough] [--control] [--keep]" >&2
		exit 2
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
	echo "usage: $0 <base-url> [<gateway-key>] [--local] [--codex] [--passthrough] [--control] [--keep]" >&2
	exit 2
fi
BASE_URL="${BASE_URL%/}"

if [ "$WANT_LOCAL" -eq 0 ] && [ "$WANT_CODEX" -eq 0 ] && [ "$WANT_PASSTHROUGH" -eq 0 ]; then
	WANT_LOCAL=1
fi

for bin in curl jq bun claude; do
	if ! command -v "$bin" >/dev/null 2>&1; then
		echo "[tool-search] FATAL: '$bin' is required but not on PATH." >&2
		exit 2
	fi
done

fail=0
note() { echo "[tool-search] $*"; }
pass() { echo "[tool-search] PASS: $*"; }
bad() {
	echo "[tool-search] FAIL: $*" >&2
	fail=1
}

# Key: the <gateway-key> arg or $GATEWAY_KEY win; otherwise re-exec under the fleet-env
# router-client consumer, which puts only GATEWAY_KEY in the environment — this script never
# opens a projection or other credential file, and never echoes the key.
if [ -z "${GATEWAY_KEY:-}" ] && [ "${FLEET_ENV_RUN:-}" != router-client ]; then
	FLEET_ENV="${FLEET_ENV_BIN:-<code-root>/fleet-tools/fleet/fleet_env/bin/fleet-env}"
	[ -x "$FLEET_ENV" ] || { echo "FATAL: fleet-env binary $FLEET_ENV is missing; refusing to run without credentials" >&2; exit 2; }
	exec "$FLEET_ENV" run router-client -- bash "${BASH_SOURCE[0]}" "$@"
fi
: "${GATEWAY_KEY:?no gateway key: pass <gateway-key>, set GATEWAY_KEY, or run under fleet-env run router-client}"

RUN_DIR=$(mktemp -d "${TMPDIR:-/tmp}/smoke-tool-search.XXXXXX") || exit 2
# shellcheck disable=SC2317  # reached through the EXIT trap, not by fallthrough
cleanup() {
	while read -r pid; do
		[ -n "$pid" ] && kill "$pid" 2>/dev/null
	done <"$RUN_DIR/proxies"
	if [ "$KEEP" -eq 1 ]; then
		note "--keep: fixture left at $RUN_DIR"
	else
		rm -rf "$RUN_DIR"
	fi
}
trap cleanup EXIT
: >"$RUN_DIR/proxies"

# ── the throwaway capture proxy (never promoted; lives and dies with this run) ───────────────
cat >"$RUN_DIR/capture-proxy.mjs" <<'PROXY'
// argv: <target-base> <out-dir> <port-file>
import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const [target, outDir, portFile] = process.argv.slice(2);
fs.mkdirSync(outDir, { recursive: true });
let seq = 0;
let counted = false;

// count_tokens for check 5, replayed in-process with the live request's own Authorization header
// so no credential ever reaches disk. Only the resulting integers are written out.
async function replayCount(url, headers, body) {
  const base = url.replace(/\/v1\/messages(\?.*)?$/, "");
  const shot = async withTools => {
    const payload = { model: body.model, system: body.system, messages: [{ role: "user", content: "hi" }] };
    if (withTools) payload.tools = body.tools;
    const r = await fetch(`${target}${base}/v1/messages/count_tokens`, {
      method: "POST",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const j = await r.json().catch(() => null);
    return typeof j?.input_tokens === "number" ? j.input_tokens : null;
  };
  try {
    const [withTools, systemOnly] = [await shot(true), await shot(false)];
    fs.writeFileSync(path.join(outDir, "count.json"), JSON.stringify({ withTools, systemOnly }));
  } catch (e) {
    fs.writeFileSync(path.join(outDir, "count.json"), JSON.stringify({ error: String(e && e.message) }));
  }
}

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on("data", c => chunks.push(c));
  req.on("end", async () => {
    const raw = Buffer.concat(chunks);
    let body = null;
    try { body = JSON.parse(raw.toString("utf8")); } catch { /* not json */ }
    const headers = { ...req.headers };
    delete headers.host;
    delete headers["content-length"];
    delete headers["accept-encoding"];
    if (body) {
      const n = String(++seq).padStart(3, "0");
      fs.writeFileSync(path.join(outDir, `${n}.req.json`), JSON.stringify({ url: req.url, body }));
      if (!counted && Array.isArray(body.tools) && body.tools.length > 0 && /\/v1\/messages$/.test(req.url.split("?")[0])) {
        counted = true;
        replayCount(req.url.split("?")[0], headers, body);
      }
    }
    let up;
    try {
      up = await fetch(target + req.url, { method: req.method, headers, body: raw.length ? raw : undefined });
    } catch (e) {
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: { message: `capture proxy could not reach ${target}: ${e && e.message}` } }));
      return;
    }
    const outHeaders = {};
    up.headers.forEach((v, k) => {
      if (k === "content-encoding" || k === "content-length" || k === "transfer-encoding") return;
      outHeaders[k] = v;
    });
    res.writeHead(up.status, outHeaders);
    if (!up.body) { res.end(); return; }
    try {
      const reader = up.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(Buffer.from(value));
      }
    } catch { /* client hung up */ }
    res.end();
  });
});
server.listen(0, "127.0.0.1", () => fs.writeFileSync(portFile, String(server.address().port)));
PROXY

# ── model resolution: whatever the gateway reports right now, never a pinned local id ────────
models_json=$(curl -s --max-time 10 -H "x-gateway-key: $GATEWAY_KEY" "$BASE_URL/v1/models") || models_json=""
model_ids=$(echo "$models_json" | jq -r '.data[]?.id // empty' 2>/dev/null)
if [ -z "$model_ids" ]; then
	echo "[tool-search] FATAL: GET $BASE_URL/v1/models returned no ids. Response: $models_json" >&2
	exit 2
fi

pick_model() { # <preferred> <listed-as> <prefix>
	if echo "$model_ids" | grep -qx "$2"; then
		echo "$1"
		return 0
	fi
	echo "$model_ids" | grep -v -E '(^|/)(qwen3[68]|Qwen3\.[68])' | grep -m1 "^$3" || true
}

# ── fixture profile (documentation of record: the spike's "Fixture profile used") ────────────
make_fixture() { # <leg> <model> <tool-search-on> <proxy-url> -> echoes the fixture home
	leg="$1"
	model="$2"
	ts_on="$3"
	proxy="$4"
	fix="$RUN_DIR/$leg"
	work="$fix/work"
	mkdir -p "$work/.claude" "$fix/.claude"

	auth_env='"ANTHROPIC_AUTH_TOKEN": "local-dummy",'
	if [ "$leg" = "passthrough" ]; then
		# The hosted leg forwards the caller's own OAuth bearer. Read-only symlink, never a copy.
		if [ ! -r "$HOME/.claude/.credentials.json" ]; then
			bad "$leg: no saved login at \$HOME/.claude/.credentials.json — the hosted leg cannot be exercised."
			return 1
		fi
		ln -s "$HOME/.claude/.credentials.json" "$fix/.claude/.credentials.json"
		auth_env=""
	fi

	cat >"$work/.claude/settings.json" <<-JSON
		{
		  "env": {
		    "ANTHROPIC_BASE_URL": "$proxy",
		    "ANTHROPIC_CUSTOM_HEADERS": "x-gateway-key: $GATEWAY_KEY",
		    "ANTHROPIC_MODEL": "$model",
		    $auth_env
		    "ENABLE_TOOL_SEARCH": "$ts_on",
		    "DISABLE_TELEMETRY": "1",
		    "DISABLE_ERROR_REPORTING": "1",
		    "DISABLE_AUTOUPDATER": "1"
		  },
		  "permissions": {
		    "deny": [
		      "Edit(//$HOME/**)",
		      "Write(//$HOME/**)",
		      "Edit(/<code-root>/**)",
		      "Write(/<code-root>/**)",
		      "WebFetch",
		      "WebSearch"
		    ],
		    "allow": ["Bash(ls)", "Bash(ls:*)"]
		  }
		}
	JSON

	# Trust must be seeded for the project root Claude Code resolves — for a /tmp working dir that
	# is the parent, not the dir itself — or permissions.allow is silently ignored.
	jq -n --arg work "$work" --arg parent "$fix" --arg tmp "${TMPDIR:-/tmp}" '{
		hasCompletedOnboarding: true,
		projects: ([$work, $parent, $tmp] | map({ (.): {
			hasTrustDialogAccepted: true,
			hasCompletedProjectOnboarding: true,
			allowedTools: []
		} }) | add)
	}' >"$fix/.claude.json"

	echo "$fix"
}

start_proxy() { # <out-dir> -> echoes the proxy base url
	out="$1"
	port_file="$out/.port"
	rm -f "$port_file"
	bun "$RUN_DIR/capture-proxy.mjs" "$BASE_URL" "$out" "$port_file" >"$out/.proxy.log" 2>&1 &
	echo "$!" >>"$RUN_DIR/proxies"
	for _ in $(seq 1 100); do
		if [ -s "$port_file" ]; then
			echo "http://127.0.0.1:$(cat "$port_file")"
			return 0
		fi
		sleep 0.1
	done
	return 1
}

# ── per-leg run ──────────────────────────────────────────────────────────────────────────────
# The built-in tool set every leg runs with, pinned so check 5's thresholds measure the
# defer_loading mechanism, not Claude Code's catalogue growth between releases (an unpinned
# catalogue drifted request 1 to 8206 > 8032 on 2026-10-19). Includes the probe tool and ToolSearch.
PINNED_TOOLS="Bash,Read,Edit,Write,ToolSearch,NotebookEdit,CronCreate,CronDelete,CronList,EnterWorktree,ExitWorktree,TaskStop"

run_leg() { # <leg> <model> <on-threshold>
	leg="$1"
	model="$2"
	threshold="$3"
	ts_on="true"
	[ "$CONTROL" -eq 1 ] && ts_on="false"

	note "── leg $leg: model=$model, ENABLE_TOOL_SEARCH=$ts_on ──"
	out="$RUN_DIR/$leg-capture"
	mkdir -p "$out"
	proxy=$(start_proxy "$out") || {
		bad "$leg: the capture proxy did not come up"
		return 1
	}
	fix=$(make_fixture "$leg" "$model" "$ts_on" "$proxy") || return 1

	answer_file="$out/.answer.json"
	(
		cd "$fix/work" || exit 2
		HOME="$fix" timeout 420 claude -p "$PROMPT" --tools "$PINNED_TOOLS" --output-format json >"$answer_file" 2>"$out/.claude.err"
	)
	rc=$?
	answer=$(jq -r '.result // empty' "$answer_file" 2>/dev/null)
	if [ "$rc" -ne 0 ] || [ -z "$answer" ]; then
		bad "$leg: \`claude -p\` returned no answer (rc=$rc). stderr: $(head -c 400 "$out/.claude.err" 2>/dev/null)"
	fi

	# request 1 = the first captured /v1/messages body for THIS leg's model that carries tools
	# (a session-title query carries none, and background haiku traffic carries another model).
	req1=""
	for f in "$out"/*.req.json; do
		[ -e "$f" ] || continue
		if jq -e --arg m "$model" '(.body.model == $m) and ((.body.tools // []) | length > 0)' "$f" >/dev/null 2>&1; then
			req1="$f"
			break
		fi
	done
	if [ -z "$req1" ]; then
		bad "$leg: no captured request carried a tools array for $model — nothing to assert on."
		return 1
	fi

	# ── 1. deferred references present in request 1 ───────────────────────────────────────────
	shape=$(jq -r --arg probe "$PROBE_TOOL" '
		.body.tools as $t
		| [
			([$t[] | select(.defer_loading == true)] | length),
			([$t[] | select(.name == "ToolSearch" and ((.input_schema // {}) | length > 0))] | length),
			([$t[] | select(.name == $probe)] | length),
			($t | length)
		] | @tsv' "$req1")
	n_defer=$(echo "$shape" | cut -f1)
	n_search=$(echo "$shape" | cut -f2)
	n_probe=$(echo "$shape" | cut -f3)
	n_tools=$(echo "$shape" | cut -f4)
	if [ "$n_defer" = "1" ] && [ "$n_search" = "1" ] && [ "$n_probe" = "0" ]; then
		pass "$leg check 1: request 1 carries $n_tools tools — one defer_loading entry, a schema'd ToolSearch, no $PROBE_TOOL"
	else
		bad "$leg check 1: request 1 ($n_tools tools) had defer_loading=$n_defer, ToolSearch=$n_search, $PROBE_TOOL=$n_probe; expected 1/1/0 (eager fallback?)"
	fi

	# ── 2. discovery call made ────────────────────────────────────────────────────────────────
	n_calls=$(jq -s '[.[] | .body.messages[]? | select(.role == "assistant") | (.content // [])[]?
		| select(.type == "tool_use" and .name == "ToolSearch")] | length' "$out"/*.req.json 2>/dev/null)
	if [ "${n_calls:-0}" -gt 0 ]; then
		pass "$leg check 2: the model emitted $n_calls ToolSearch tool_use block(s)"
	else
		bad "$leg check 2: no ToolSearch tool_use block was ever emitted — the model was never told what to search for, or chose not to"
	fi

	# ── 3. references relayed and the catalogue grew by exactly one full-schema entry ─────────
	grown=$(jq -s -r --arg probe "$PROBE_TOOL" --argjson base "$n_tools" '
		[ .[]
		  | select([ .body.messages[]? | (.content // [])[]? | select(.type == "tool_result")
		             | (if (.content | type) == "array" then .content[] else empty end)
		             | select(.type == "tool_reference") ] | length > 0)
		  | select(((.body.tools // []) | length) == ($base + 1))
		  | [ ((.body.tools // []) | length),
		      ([ (.body.tools // [])[] | select(.name == $probe and ((.input_schema // {}) | length > 0) and .defer_loading == true) ] | length) ]
		] | if length == 0 then "none" else (.[0] | @tsv) end' "$out"/*.req.json 2>/dev/null)
	if [ "$grown" = "none" ]; then
		bad "$leg check 3: no request carried a tool_reference tool_result together with a catalogue grown to $((n_tools + 1)) entries"
	else
		grown_n=$(echo "$grown" | cut -f1)
		grown_probe=$(echo "$grown" | cut -f2)
		if [ "$grown_probe" = "1" ]; then
			pass "$leg check 3: the tool_reference was relayed and tools grew $n_tools → $grown_n with $PROBE_TOOL schema'd and defer_loading retained"
		else
			bad "$leg check 3: tools grew to $grown_n but $PROBE_TOOL was not present with both a full input_schema and defer_loading:true"
		fi
	fi

	# ── 4. the concrete tool's schema reached the answer ──────────────────────────────────────
	n_params=$(echo "$answer" | grep -oiE "$PROBE_PARAMS_RE" | sort -u | wc -l)
	if [ "$n_params" -ge 2 ]; then
		pass "$leg check 4: the final answer names $n_params of $PROBE_TOOL's own parameters"
	else
		bad "$leg check 4: the final answer named $n_params $PROBE_TOOL parameter(s) — the concrete schema never reached the model. Answer: $(echo "$answer" | head -c 300)"
	fi

	# ── 5. within-leg token gate ──────────────────────────────────────────────────────────────
	tok=$(jq -r '.withTools // empty' "$out/count.json" 2>/dev/null)
	sys_only=$(jq -r '.systemOnly // empty' "$out/count.json" 2>/dev/null)
	if [ -z "$tok" ]; then
		bad "$leg check 5: count_tokens replay produced no integer. $(head -c 300 "$out/count.json" 2>/dev/null)"
	elif [ "$CONTROL" -eq 1 ]; then
		if [ "$tok" -gt "$threshold" ]; then
			pass "$leg check 5 (control): the eager catalogue cost $tok system+tools tokens, above the WP-5 on-threshold $threshold"
		else
			bad "$leg check 5 (control): the eager catalogue cost only $tok tokens, not above the WP-5 on-threshold $threshold — the control is no longer a control"
		fi
	elif [ "$tok" -le "$((threshold * 110 / 100))" ]; then
		pass "$leg check 5: request 1 cost $tok system+tools tokens (tools block $((tok - ${sys_only:-0}))), within the WP-5 on-threshold $threshold +10%"
	else
		bad "$leg check 5: request 1 cost $tok system+tools tokens, above the WP-5 on-threshold $threshold +10% ($((threshold * 110 / 100))) — the catalogue is being eagerly expanded"
	fi
}

# ── legs ─────────────────────────────────────────────────────────────────────────────────────
if [ "$WANT_LOCAL" -eq 1 ]; then
	m=$(pick_model "claude-local/nemotron-4b" "claude-local/nemotron-4b" "claude-local/")
	if [ -z "$m" ]; then
		bad "local: /v1/models lists no claude-local/* id"
	else
		run_leg "local" "$m" 3983
	fi
fi

if [ "$WANT_CODEX" -eq 1 ]; then
	note "--codex passed: this bills one turn on the operator's Codex subscription."
	codex_test=$(codex_test_model) || exit 2
	m=$(pick_model "codex-b/$codex_test" "codex-b/$codex_test" "codex-b/")
	if [ -z "$m" ]; then
		bad "codex: /v1/models lists no codex-b/* id"
	else
		run_leg "codex" "$m" 3529
	fi
else
	note "--codex not passed: SKIPPING the metered codex sidecar leg (one Codex-subscription turn)."
fi

if [ "$WANT_PASSTHROUGH" -eq 1 ]; then
	note "--passthrough passed: this bills one turn on the operator's Claude subscription."
	m=$(pick_model "claude-haiku-4-5" "anthropic/claude-haiku-4-5" "anthropic/")
	if [ -z "$m" ]; then
		bad "passthrough: /v1/models lists no anthropic/* id"
	else
		run_leg "passthrough" "$m" 11470
	fi
else
	note "--passthrough not passed: SKIPPING the metered hosted leg (one Claude-subscription turn)."
fi

echo
if [ "$CONTROL" -eq 1 ]; then
	if [ "$fail" -eq 0 ]; then
		echo "[tool-search] CONTROL FAILURE: every conformance check passed against an eager ENABLE_TOOL_SEARCH=false profile." >&2
		echo "[tool-search] The gate cannot fail, so it is not a gate. Exiting non-zero." >&2
		exit 1
	fi
	echo "[tool-search] control mode: the conformance checks failed against the eager profile, as they must."
	echo "[tool-search] exiting 1 — a non-zero exit IS the expected outcome of --control."
	exit 1
fi

if [ "$fail" -eq 0 ]; then
	echo "[tool-search] all checks passed"
	exit 0
else
	echo "[tool-search] one or more checks FAILED" >&2
	exit 1
fi
