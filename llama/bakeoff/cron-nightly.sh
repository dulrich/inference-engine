#!/usr/bin/env bash
# Nightly unattended bake-off queue window (plans/longcontext-pow2-overnight-campaign.md WP-6;
# thinned to an arm-gated shim by plans/bakeoff-queue-control-plane.md WP-3).
#
# Fail-closed, nightly-only arm gate (req 5): no `host/armed` marker -> log "disarmed", exit 0
# without touching Docker/the queue/anything else. `armed` is dispatcher-owned (created/removed
# only by an `arm`/`disarm` control request, plan "Ownership table") -- this script only READS
# it. A LIVE manual run in progress at invocation time is not this script's concern: `run-
# window.sh` (below) owns admission via its own non-blocking `cron.lock` acquire (busy -> result
# `stage:"busy"`, exit 0, next night applies) -- the crontab line that invokes this script drops
# its own outer `flock` (the wrapper now owns the lock, one owner, one place; see
# llama/bakeoff/README.md's Scheduling section for the exact installed crontab line and the
# CURRENTLY DISARMED note carried over from the pre-control-plane convention).
#
# Queue deadlines are UTC wall-clock (batch.py's now_fn is datetime.now(timezone.utc)) -- a queue
# file's "deadline" must be authored in UTC. The queue spans multiple nights by design: each
# invocation resumes pending cells, stops launching new ones at the deadline, and no-ops once the
# queue is terminal.
#
# Exit status: whatever run-window.sh returns for this invocation (0 on a clean/no-op/disarmed
# night; nonzero when the night needs attention -- see run-window.sh's own header).

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
BAKEOFF_DIR="${SCRIPT_DIR}"
QUEUE="${1:-${BAKEOFF_DIR}/queues/ladder-v1.json}"

export PATH="/usr/local/bin:/usr/bin:/bin"

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

STATE_DIR="${BAKEOFF_STATE_DIR:-${HOME}/.local/state/inference-bakeoff}"
export BAKEOFF_STATE_DIR="${STATE_DIR}"
ARMED_MARKER="${STATE_DIR}/control/host/armed"

log "=== cron-nightly start (queue=${QUEUE})"

if [ ! -e "${ARMED_MARKER}" ]; then
    log "disarmed: ${ARMED_MARKER} absent -- exiting without running (arm via POST /api/control/arm or a control request)"
    exit 0
fi

REQUEST_ID="nightly-$(date -u '+%Y%m%d')"
log "armed: invoking run-window.sh (request-id=${REQUEST_ID})"
# `setsid` here (not just `exec`) guarantees pid = pgid = sid for run-window.sh regardless of
# whether cron itself already puts each job in its own session -- the SAME group-identity
# invariant the dispatcher's own manual-start launch path relies on (control_dispatcher.py
# `_default_launch`), so the dispatcher's group-liveness check behaves identically for both entry
# points.
exec setsid "${BAKEOFF_DIR}/run-window.sh" "${QUEUE}" --request-id "${REQUEST_ID}"
