# shellcheck shell=bash
# test-models.sh — sourced by the router smoke scripts: the live codex test model, read from
# router/test/test-models.json (the same file test/test-models.mjs reads); $CODEX_TEST_MODEL
# overrides it for one run. Defines a function only; sourcing has no side effect.

codex_test_model() { # prints the bare codex model id (no persona prefix); non-zero on a bad config
	local file model src
	file="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/test-models.json"
	model="${CODEX_TEST_MODEL:-}"
	src="the CODEX_TEST_MODEL override"
	if [ -z "$model" ]; then
		src=$file
		model=$(jq -r '.codex // empty' "$file" 2>/dev/null)
	fi
	case "$model" in
	"" | */*)
		echo "test-models: codex model must be a bare id without a persona prefix, got '${model}' ($src)" >&2
		return 1
		;;
	esac
	printf '%s\n' "$model"
}
