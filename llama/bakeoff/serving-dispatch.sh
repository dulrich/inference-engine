#!/usr/bin/env bash
# Serving-control dispatcher wrapper (plans/bench-dashboard-serving-control.md WP-5) -- the thing
# the operator's minutely crontab line actually runs.
#
# Its whole job is credentials. `scripts/sync-openwebui-capabilities.py --apply` needs
# OPENWEBUI_URL and OPENWEBUI_API_KEY, which have no default by design, and a cron line has no
# useful environment. So: source them from a 0600 file OUTSIDE the repo, then exec the dispatcher,
# which inherits them untouched.
#
# This script never echoes, logs, or passes a variable VALUE anywhere. The key is passed by
# environment only -- never on a command line, where it would be visible in process listings
# (the sync script's own epilog warns about exactly that).
#
# Install: see llama/bakeoff/README.md, "Serving control". The crontab line and the env file are
# installed by the OPERATOR, never by an agent.
set -euo pipefail

ENV_FILE="${INFERENCE_SERVING_ENV:-$HOME/.config/inference-serving/env}"

if [ -e "$ENV_FILE" ]; then
    if [ ! -f "$ENV_FILE" ]; then
        echo "serving-dispatch: $ENV_FILE is not a regular file; refusing to source it" >&2
        exit 2
    fi
    mode="$(stat -c '%a' "$ENV_FILE")"
    owner="$(stat -c '%u' "$ENV_FILE")"
    if [ "$mode" != "600" ]; then
        echo "serving-dispatch: $ENV_FILE has mode $mode; it holds OPENWEBUI_API_KEY and must be 0600" >&2
        exit 2
    fi
    if [ "$owner" != "$(id -u)" ]; then
        echo "serving-dispatch: $ENV_FILE is not owned by the invoking user; refusing to source it" >&2
        exit 2
    fi
    # `set -a` exports everything the file assigns, which is how the dispatcher's sync subprocess
    # inherits it. Values are never printed -- not here, not by the dispatcher.
    set -a
    # The path is operator-configured and deliberately outside the repo, so it cannot be
    # followed at lint time.
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# Resolved from this script's own real path, never the cwd: a cron line has no working directory.
exec python3 "$(dirname "$(readlink -f "$0")")/serving_dispatcher.py" dispatch "$@"
