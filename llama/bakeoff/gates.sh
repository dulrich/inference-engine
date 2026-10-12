#!/usr/bin/env bash
# Bake-off gate runner -- the executable form of llama/bakeoff/AGENTS.md's gate list.
#
# Runs from anywhere; every gate is resolved against this script's own directory and the repo
# root, because two of them are path-sensitive:
#   * the unittest suite is discovered by path, and
#   * `sweep.py --dry-run` echoes the spec path it was given into its own JSON output, so the
#     dry-run digest below is only comparable when the spec is named from the repo root.
#     (Running it from inside llama/bakeoff/ yields a different, equally "passing" digest.)
#
# Ruff and Pyright are DEV-ONLY, via uv. The harness runtime stays Python-stdlib-only and is
# always invoked with the host's bare `python3` -- never through .venv.
set -euo pipefail

BAKEOFF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${BAKEOFF_DIR}/../.." && pwd)"

# Pinned Pyright baseline: the number of errors `basic` reports over the production modules
# today. The gate fails if the count RISES. See AGENTS.md "The Pyright ratchet" for why this
# is a count rather than zero, and for the steps that retire it.
PYRIGHT_BASELINE="${PYRIGHT_BASELINE:-0}"

fail=0
step() { printf '\n=== %s ===\n' "$1"; }

cd "${REPO_ROOT}"

step "unittest suite (summary is on STDERR; stdout is the suite's own JSON)"
python3 -m unittest discover -s llama/bakeoff/tests -p 'test_*.py' 2>&1 >/dev/null | tail -4 || fail=1

step "py_compile"
python3 -m py_compile llama/bakeoff/*.py && echo "py_compile: clean" || fail=1

step "deterministic dry runs"
sweep_digest="$(python3 llama/bakeoff/sweep.py --dry-run llama/bakeoff/examples/sweep-example.json | sha256sum | cut -c1-16)"
lc_digest="$(python3 llama/bakeoff/longcontext.py --dry-run --target-tokens 31000 | sha256sum | cut -c1-16)"
echo "sweep      ${sweep_digest} (expect 3880a0a63ccd1019)"
echo "longcontext ${lc_digest} (expect 3c51046cf8e55f55)"
[ "${sweep_digest}" = "3880a0a63ccd1019" ] || { echo "sweep dry-run digest MOVED"; fail=1; }
[ "${lc_digest}" = "3c51046cf8e55f55" ] || { echo "longcontext dry-run digest MOVED"; fail=1; }

cd "${BAKEOFF_DIR}"

step "ruff"
uv run ruff check . && echo "ruff: clean" || fail=1

step "pyright (production modules; ratchet baseline ${PYRIGHT_BASELINE})"
# Pyright exits non-zero whenever it reports an error, which is the expected steady state
# while the ratchet baseline is above zero -- so read the count out of its JSON and judge on
# that, never on its exit status. `|| true` keeps `set -o pipefail` from aborting the run here.
pyright_json="$(uv run pyright --outputjson 2>/dev/null || true)"
pyright_errors="$(printf '%s' "${pyright_json}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["summary"]["errorCount"])')"
echo "pyright errors: ${pyright_errors} (baseline ${PYRIGHT_BASELINE})"
if [ "${pyright_errors}" -gt "${PYRIGHT_BASELINE}" ]; then
    echo "pyright count ROSE above the pinned baseline -- fix the new finding, do not raise the baseline"
    fail=1
elif [ "${pyright_errors}" -lt "${PYRIGHT_BASELINE}" ]; then
    echo "pyright count FELL -- ratchet it down: set PYRIGHT_BASELINE=${pyright_errors} in this script"
fi

step "text-file hygiene (no raw NUL bytes)"
for f in "${BAKEOFF_DIR}"/*.py "${BAKEOFF_DIR}"/*.sh "${BAKEOFF_DIR}"/*.toml "${BAKEOFF_DIR}"/tests/*.py; do
    # `grep -q` exits 1 on the (desired) no-match, so test it as a condition -- never let it
    # be the last command in the loop body under `set -e`.
    if LC_ALL=C grep -qP '\0' "${f}"; then
        echo "NUL byte in ${f}"
        fail=1
    fi
done
echo "hygiene: clean"

printf '\n'
[ "${fail}" -eq 0 ] && echo "ALL BAKE-OFF GATES GREEN" || echo "BAKE-OFF GATES FAILED"
exit "${fail}"
