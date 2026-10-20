#!/usr/bin/env bash
# compat shim until the crontab is repointed to `control/serving-dispatch.sh`;
# plans/serving-control-redesign.md WP-1
exec "$(dirname "$(readlink -f "$0")")/../../control/serving-dispatch.sh" "$@"
