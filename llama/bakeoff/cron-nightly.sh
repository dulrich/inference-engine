#!/usr/bin/env bash
# Nightly unattended bake-off queue window (plans/longcontext-pow2-overnight-campaign.md WP-6).
#
# Invoked from the user crontab under `flock` (see llama/README.md "Scheduling" for the exact
# installed line -- flock serializes this script against itself; GPU exclusivity against the
# router is batch.py's own container stop/start, a separate mechanism by design). Runs
# `batch run` and THEN `batch verify` on the run directory, persisting both report forms
# (verify_report.json + verify_report.md, written by verify_run.py into the run dir) -- the
# run-then-verify pairing is audit M2's requirement, not an optional nicety.
#
# Queue deadlines are UTC wall-clock (batch.py's now_fn is datetime.now(timezone.utc)) -- a
# queue file's "deadline" must be authored in UTC (WP-5 finding #4; 12:00 UTC = 07:00 CDT /
# 06:00 CST). The queue spans multiple nights by design: each invocation resumes pending
# cells, stops launching new ones at the deadline, and no-ops once the queue is terminal.
#
# Exit status: 0 on a clean night (including a terminal-queue no-op); NONZERO when the night
# needs attention -- a hang-abort, a crashed/failed cell, or a queue/batch error. verify's own
# exit 1 means honest findings in the reports, not breakage (WP-5), and is surfaced as a
# NEEDS-ATTENTION log line rather than a failure exit on its own.

set -uo pipefail

REPO_DIR="."
BAKEOFF_DIR="${REPO_DIR}/llama/bakeoff"
QUEUE="${1:-${BAKEOFF_DIR}/queues/ladder-v1.json}"
QUEUE_STEM="$(basename "${QUEUE}" .json)"
RUN_DIR="${REPO_DIR}/runs/bakeoff/${QUEUE_STEM}"
STATE_FILE="$(dirname "${QUEUE}")/${QUEUE_STEM}.state.json"

# Scratch sibling container (WP-5 finding #1: the maintenance window stops the router container,
# so scratch servers exec into a SIBLING with the same image/GPU devices/model mounts and no
# port publish). Created here if absent, started if stopped -- crash-tolerant either way.
SCRATCH_CONTAINER="bakeoff-scratch"
SCRATCH_IMAGE="crucible-llamacpp-vulkan:b10534-swap243"

export PATH="/usr/local/bin:/usr/bin:/bin"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

log "=== cron-nightly start (queue=${QUEUE})"

if ! docker inspect -f '{{.State.Running}}' "${SCRATCH_CONTAINER}" 2>/dev/null | grep -q true; then
    if docker inspect "${SCRATCH_CONTAINER}" >/dev/null 2>&1; then
        log "scratch container exists but is stopped -- starting"
        docker start "${SCRATCH_CONTAINER}" >/dev/null || { log "ERROR: docker start failed"; exit 1; }
    else
        log "creating scratch container ${SCRATCH_CONTAINER}"
        docker run -d --name "${SCRATCH_CONTAINER}" \
            --device /dev/dri --group-add 992 --group-add 44 \
            -v /srv/models:/models:ro \
            "${SCRATCH_IMAGE}" sleep infinity >/dev/null \
            || { log "ERROR: docker run failed"; exit 1; }
    fi
fi

mkdir -p "${RUN_DIR}"
python3 "${BAKEOFF_DIR}/batch.py" run \
    --queue "${QUEUE}" \
    --scratch-container "${SCRATCH_CONTAINER}" \
    --records-dir "${RUN_DIR}"
run_exit=$?
log "batch run exit=${run_exit}"

# Verifier state discovery expects <run-dir>/queue.state.json (WP-5); the runner writes its
# state beside the queue file, so copy the current snapshot in before verifying.
if [ -f "${STATE_FILE}" ]; then
    cp "${STATE_FILE}" "${RUN_DIR}/queue.state.json"
fi

python3 "${BAKEOFF_DIR}/batch.py" verify --run "${RUN_DIR}"
verify_exit=$?
log "batch verify exit=${verify_exit} (reports: ${RUN_DIR}/verify_report.{json,md})"

needs_attention=0
if [ "${run_exit}" -ne 0 ]; then
    # batch run is nonzero only for aborted-hang (exit 1) or an interrupted run (130) --
    # both are unconditional operator-attention conditions, never auto-retried.
    log "NEEDS-ATTENTION: batch run exited ${run_exit} (hang-abort or interruption)"
    needs_attention=1
fi
if [ -f "${STATE_FILE}" ] && grep -Eq '"state": *"(failed|aborted-hang)"' "${STATE_FILE}"; then
    # Crash recovery marks interrupted cells failed and NEVER silently re-runs them (WP-1
    # rule); a failed cell may also be a genuine correctness finding. Either way a human
    # (or the verifier agent) looks before anything is re-armed (batch run --re-arm-cell).
    log "NEEDS-ATTENTION: failed/aborted cells present in ${STATE_FILE}"
    needs_attention=1
fi
if [ "${verify_exit}" -ne 0 ]; then
    log "NEEDS-ATTENTION: verify reported findings (exit ${verify_exit}) -- read the reports"
    needs_attention=1
fi

log "=== cron-nightly done (needs_attention=${needs_attention})"
if [ "${run_exit}" -ne 0 ]; then exit "${run_exit}"; fi
exit 0
