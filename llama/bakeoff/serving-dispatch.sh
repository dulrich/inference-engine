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

# fleet-env cutover switch (plans/env-management.md). 0 = ON, 1 = OFF; exits loudly if
# ON but the fleet-env binary is missing or the switch is malformed (never a silent
# fallback to the legacy file).
FLEET_ENV="${FLEET_ENV_BIN:-<code-root>/fleet-tools/fleet_env/bin/fleet-env}"
fleet_env_on() {
  local rc=0
  if [[ ! -x "$FLEET_ENV" ]]; then
    case "${FLEET_ENV_CUTOVER:-}" in
      0) return 1 ;;
      1) ;;
      *) [[ -e "${FLEET_ENV_HOME:-$HOME/.config/fleet-env}/cutover" ]] || return 1 ;;
    esac
    printf '%s: fleet-env cutover is on but %s is missing; refusing the legacy env path\n' "$0" "$FLEET_ENV" >&2
    exit 1
  fi
  fleet_env_exec enabled || rc=$?
  if [[ $rc -gt 1 ]]; then
    printf '%s: fleet-env enabled failed (exit %d)\n' "$0" "$rc" >&2
    exit 1
  fi
  return "$rc"
}
# fleet-env is a node script and cron's PATH has no node: same nvm link as below, scoped to
# the fleet-env call only so the OFF path's environment is unchanged.
fleet_env_exec() {
    if [ -x "$HOME/.nvm/current/bin/node" ]; then
        PATH="$HOME/.nvm/current/bin:$PATH" "$FLEET_ENV" "$@"
    else
        "$FLEET_ENV" "$@"
    fi
}

# Cutover ON: OPENWEBUI_URL/OPENWEBUI_API_KEY come from the fleet-env store -- re-exec this
# wrapper under `fleet-env run serving-dispatch` (no file) and skip the legacy env file below.
# OFF (default): the legacy ~/.config/inference-serving/env path, unchanged.
under_fleet_env=0
if [ "${FLEET_ENV_RUN:-}" = serving-dispatch ]; then
    under_fleet_env=1
elif fleet_env_on; then
    if [ -x "$HOME/.nvm/current/bin/node" ]; then PATH="$HOME/.nvm/current/bin:$PATH"; export PATH; fi
    exec "$FLEET_ENV" run serving-dispatch -- "$(readlink -f "$0")" "$@"
fi

ENV_FILE="${INFERENCE_SERVING_ENV:-$HOME/.config/inference-serving/env}"

if [ "$under_fleet_env" = 0 ] && [ -e "$ENV_FILE" ]; then
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

# The dispatcher's reconciliation legs shell out to `node` (router/model-picker-lineup.mjs) and
# `python3`; cron's PATH has neither the nvm node dir nor ~/.local/bin. Prefer the version-stable
# ~/.nvm/current link (the same one sidecar/install.sh renders against), so a node bump never
# strands the cron on a deleted version dir.
if [ -x "$HOME/.nvm/current/bin/node" ]; then
    PATH="$HOME/.nvm/current/bin:$PATH"
    export PATH
fi

# Resolved from this script's own real path, never the cwd: a cron line has no working directory.
exec python3 "$(dirname "$(readlink -f "$0")")/serving_dispatcher.py" dispatch "$@"
