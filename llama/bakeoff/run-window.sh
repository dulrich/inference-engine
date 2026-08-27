#!/usr/bin/env bash
# One bake-off run window (plans/bakeoff-queue-control-plane.md WP-3) -- extracted from
# cron-nightly.sh, which is now a thin arm-gated shim over this script (see cron-nightly.sh's own
# header). Runs ONE queue window under the run lock (`cron.lock`, held for this process's whole
# process-group lifetime -- see the flock step below) with full evidence published for the host
# dispatcher (`control_dispatcher.py dispatch`) to read: this is "the wrapper" in the plan's
# design -- it owns liveness and per-invocation launch results; `batch.py` stays a pure queue
# executor underneath it.
#
# Usage: run-window.sh <queue.json> [--minutes N] --request-id <invocation-id>
#   <queue.json>      path to the queue file to run.
#   --minutes N       optional -- overrides the queue's own HH:MM deadline with `start + N
#                      minutes` for THIS invocation only (batch.py's `--deadline-minutes`; the
#                      manual-window mechanism). Omitted for the nightly, which keeps the queue's
#                      own deadline.
#   --request-id ID   REQUIRED -- the invocation id: a manual start's own request id
#                      (req-...), or `nightly-<UTCyyyymmdd>` for the scheduled nightly invocation
#                      (computed by cron-nightly.sh, this script's only other caller besides the
#                      dispatcher itself).
#
# Spawned via `setsid` by the dispatcher (`control_dispatcher.py`'s `_default_launch`) so this
# process's pid = pgid = sid -- the flock fd opened below is inherited by every child this script
# forks (batch.py, its own scratch-server docker calls), so THE RUN LOCK'S LIFETIME IS THE PROCESS
# GROUP'S LIFETIME, matching the dispatcher's own group-liveness definition exactly (plan
# "Admission policy": "whoever acquires the run lock non-blocking runs; everyone else records
# busy and exits -- nothing waits, nothing queues, no retry").
#
# The 8-step sequence below is LOAD-BEARING AND ORDERED (plan "Wrapper" section, cycle-1 B1/B3) --
# do not reorder without re-reading that section first.
#
# Every docker/process-touching sub-step is behind an env-var override, all defaulting to the
# real behavior, so this exact script is what the integration test (tests/test_run_window*.py)
# drives for real (no GPU, no docker, no live operations) -- see each `BAKEOFF_*_CMD` below.
#   BAKEOFF_STATE_DIR          -- state-dir root (default ~/.local/state/inference-bakeoff).
#   BAKEOFF_BATCH_CMD          -- base command for `batch.py run`/`batch.py verify`
#                                  (default "python3 <this-dir>/batch.py").
#   BAKEOFF_ENSURE_SCRATCH_CMD -- replaces the whole "ensure scratch container" preamble
#                                  (step 6) when set; unset uses the real docker inspect/start/run
#                                  logic moved here from cron-nightly.sh.
#   BAKEOFF_ORPHAN_RECOVER_CMD -- replaces the whole orphan-workload-recovery call (step 4) when
#                                  set; unset uses the real
#                                  `control_dispatcher.py orphan-recover` (sweep.py's docker
#                                  top/os.kill seam).
#   BAKEOFF_SCRATCH_CONTAINER  -- default "bakeoff-scratch".
#   BAKEOFF_SCRATCH_IMAGE      -- default "crucible-llamacpp-vulkan:b10534-swap243".
#   BAKEOFF_RUNS_DIR           -- base dir for `--records-dir` (default <repo>/llama/bakeoff/records).
#   BAKEOFF_PORT               -- scratch-server port (default 8899, batch.py's own default).

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
BAKEOFF_DIR="${SCRIPT_DIR}"
REPO_DIR="$(cd -- "${SCRIPT_DIR}/../.." &>/dev/null && pwd)"

export PATH="/usr/local/bin:/usr/bin:/bin"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

QUEUE=""
MINUTES=""
REQUEST_ID=""
while [ $# -gt 0 ]; do
    case "$1" in
        --minutes)
            MINUTES="$2"; shift 2 ;;
        --request-id)
            REQUEST_ID="$2"; shift 2 ;;
        *)
            if [ -z "${QUEUE}" ]; then QUEUE="$1"; else log "ERROR: unexpected argument '$1'"; exit 2; fi
            shift ;;
    esac
done

if [ -z "${QUEUE}" ]; then log "ERROR: missing required <queue.json> argument"; exit 2; fi
if [ -z "${REQUEST_ID}" ]; then log "ERROR: missing required --request-id"; exit 2; fi

QUEUE_STEM="$(basename "${QUEUE}" .json)"
STATE_FILE="$(dirname "${QUEUE}")/${QUEUE_STEM}.state.json"

STATE_DIR="${BAKEOFF_STATE_DIR:-${HOME}/.local/state/inference-bakeoff}"
export BAKEOFF_STATE_DIR="${STATE_DIR}"
CONTROL_DIR="${STATE_DIR}/control"
CRON_LOCK="${STATE_DIR}/cron.lock"
STOP_MARKER="${CONTROL_DIR}/stop-requested.${REQUEST_ID}"

BATCH_CMD="${BAKEOFF_BATCH_CMD:-python3 ${BAKEOFF_DIR}/batch.py}"
SCRATCH_CONTAINER="${BAKEOFF_SCRATCH_CONTAINER:-bakeoff-scratch}"
SCRATCH_IMAGE="${BAKEOFF_SCRATCH_IMAGE:-crucible-llamacpp-vulkan:b10534-swap243}"
RUNS_DIR="${BAKEOFF_RUNS_DIR:-${REPO_DIR}/llama/bakeoff/records}"
RUN_DIR="${RUNS_DIR}/${QUEUE_STEM}"
PORT="${BAKEOFF_PORT:-8899}"

CTL() {
    # `--state-dir` is a per-subcommand argument (argparse subparsers), so it must come AFTER
    # the subcommand name, not before it.
    local subcmd="$1"; shift
    python3 "${BAKEOFF_DIR}/control_dispatcher.py" "${subcmd}" --state-dir "${STATE_DIR}" "$@"
}

log "=== run-window start (queue=${QUEUE} request-id=${REQUEST_ID} minutes=${MINUTES:-<queue deadline>})"

# --- Step 1: FIRST ACTION -- publish full process identity before anything else (cycle-2 B1: a
# wrapper that dies before ever publishing live.json must still be testable for death). $$ is
# both this process's pid AND pgid when spawned via `setsid` (dispatcher path); a direct manual
# invocation for local testing gets the same equality via the shell's own pgid.
mkdir -p "${CONTROL_DIR}/host/results"
CTL result-write --invocation-id "${REQUEST_ID}" --stage wrapper-started --pid "$$" --pgid "$$"

# --- Step 2: revocation check -- the dispatcher may already have terminalized this id (the
# residual width of steps 1-4 the plan calls out); exit without running if so.
if [ -f "${CONTROL_DIR}/host/dispositions/${REQUEST_ID}.json" ]; then
    log "revoked: dispositions/${REQUEST_ID}.json already exists -- exiting without running"
    exit 0
fi

# --- Step 3: acquire the run lock (non-blocking). The flock fd (200) is inherited by every child
# this script forks below -- THE LOCK'S LIFETIME IS THE PROCESS GROUP'S LIFETIME.
exec 200>"${CRON_LOCK}"
if ! flock -n 200; then
    log "busy: cron.lock held by another run -- exiting"
    CTL result-write --invocation-id "${REQUEST_ID}" --stage busy
    exit 0
fi

# --- Step 4: window-start hygiene -- clear any stale marker bound to OUR OWN invocation id
# (batch.py deliberately has NO stale-clear of its own), then orphan-workload recovery: a prior
# crashed run's `llama-server` is daemon-parented OUTSIDE this process group
# (sweep.spawn_scratch_server: `docker exec -d` + internal `nohup`), so it must be torn down here
# before any run is admitted against an uncleared prior workload -- symmetric with the
# dispatcher's own janitor.
CTL clear-stop-marker --invocation-id "${REQUEST_ID}"

if [ -n "${BAKEOFF_ORPHAN_RECOVER_CMD:-}" ]; then
    # shellcheck disable=SC2086
    ${BAKEOFF_ORPHAN_RECOVER_CMD}
else
    CTL orphan-recover --port "${PORT}" --container "${SCRATCH_CONTAINER}" >/dev/null || true
fi

# --- Step 5: publish liveness, advance the result stage.
CTL live-publish --invocation-id "${REQUEST_ID}" --request-id "${REQUEST_ID}" \
    --queue-stem "${QUEUE_STEM}" --pid "$$" --pgid "$$"
CTL result-write --invocation-id "${REQUEST_ID}" --stage running

# --- Step 6: ensure the scratch sibling container, then run the queue (logic moved here from
# cron-nightly.sh -- WP-5 finding #1: the maintenance window stops the ROUTER container, but
# sweep.scratch_server `docker exec`s into its target, so scratch servers need a running sibling).
if [ -n "${BAKEOFF_ENSURE_SCRATCH_CMD:-}" ]; then
    # shellcheck disable=SC2086
    ${BAKEOFF_ENSURE_SCRATCH_CMD}
else
    if ! docker inspect -f '{{.State.Running}}' "${SCRATCH_CONTAINER}" 2>/dev/null | grep -q true; then
        if docker inspect "${SCRATCH_CONTAINER}" >/dev/null 2>&1; then
            log "scratch container exists but is stopped -- starting"
            docker start "${SCRATCH_CONTAINER}" >/dev/null || log "ERROR: docker start failed"
        else
            log "creating scratch container ${SCRATCH_CONTAINER}"
            docker run -d --name "${SCRATCH_CONTAINER}" \
                --device /dev/dri --group-add 992 --group-add 44 \
                -v /srv/models:/models:ro \
                "${SCRATCH_IMAGE}" sleep infinity >/dev/null \
                || log "ERROR: docker run failed"
        fi
    fi
fi

mkdir -p "${RUN_DIR}"
DEADLINE_ARGS=()
if [ -n "${MINUTES}" ]; then DEADLINE_ARGS=(--deadline-minutes "${MINUTES}"); fi

# shellcheck disable=SC2086
${BATCH_CMD} run \
    --queue "${QUEUE}" \
    --scratch-container "${SCRATCH_CONTAINER}" \
    --records-dir "${RUN_DIR}" \
    --port "${PORT}" \
    --stop-marker "${STOP_MARKER}" \
    "${DEADLINE_ARGS[@]}"
run_exit=$?
log "batch run exit=${run_exit}"

# --- Step 7: post-run cleanup -- a stop delivered during the final cell is inert by design
# (batch.py's own completion branch already consumes it; this is belt-and-suspenders for
# anything arriving in the narrow window after batch.py's process has exited but before verify
# starts), then advance liveness to "verifying" and run the verifier.
CTL clear-stop-marker --invocation-id "${REQUEST_ID}"
CTL live-set-phase --phase verifying

if [ -f "${STATE_FILE}" ]; then
    cp "${STATE_FILE}" "${RUN_DIR}/queue.state.json"
fi

# shellcheck disable=SC2086
${BATCH_CMD} verify --run "${RUN_DIR}"
verify_exit=$?
log "batch verify exit=${verify_exit} (reports: ${RUN_DIR}/verify_report.{json,md})"

# --- Step 8: publish exit evidence and clear liveness.
CTL result-write --invocation-id "${REQUEST_ID}" --stage exited \
    --run-exit "${run_exit}" --verify-exit "${verify_exit}"
CTL live-clear

needs_attention=0
if [ "${run_exit}" -ne 0 ]; then
    log "NEEDS-ATTENTION: batch run exited ${run_exit} (hang-abort or interruption)"
    needs_attention=1
fi
if [ -f "${STATE_FILE}" ] && grep -Eq '"state": *"(failed|aborted-hang)"' "${STATE_FILE}"; then
    log "NEEDS-ATTENTION: failed/aborted cells present in ${STATE_FILE}"
    needs_attention=1
fi
if [ "${verify_exit}" -ne 0 ]; then
    log "NEEDS-ATTENTION: verify reported findings (exit ${verify_exit}) -- read the reports"
    needs_attention=1
fi

log "=== run-window done (request-id=${REQUEST_ID} needs_attention=${needs_attention})"
if [ "${run_exit}" -ne 0 ]; then exit "${run_exit}"; fi
exit 0
