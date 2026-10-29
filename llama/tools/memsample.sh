#!/usr/bin/env bash
# plans/qwen4exp-ram-ab-spike.md WP-1 -- the RAM A/B window's /proc sampler.
#
# Usage: memsample.sh <out-path> [interval-s]   (interval defaults to 10)
#
# Scans /proc directly for llama-server processes -- never `pgrep`, `ps`, or `pidof`. The
# campaign's scratch image lacks those tools, and even on the host they would not help: the
# `llama-server` this sampler must see runs inside the `bakeoff-scratch` container, and only a
# direct read of `/proc/<pid>/comm` from the host's own pid namespace finds it there (root
# CLAUDE.md's "healthy service has told you nothing" quirk family; llama/AGENTS.md). Every
# `interval` seconds until killed (SIGTERM/SIGINT exit this cleanly, code 0) it appends one line
# to <out-path> with the host's /proc/meminfo and /proc/vmstat swap counters, plus, for every
# process whose exact `comm` is `llama-server`, its per-pid VmRSS/RssFile/RssAnon/VmSwap. A pid
# that exits between the `comm` read and the `status` read is skipped, never treated as an error
# -- processes come and go under a live queue run and a vanished pid must not kill the sampler.

for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '<out-path> [interval-s]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/build-id.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

set -euo pipefail

out_path="${1:?usage: memsample.sh <out-path> [interval-s]}"
interval="${2:-10}"

sleep_pid=""

cleanup_and_exit() {
    if [[ -n "$sleep_pid" ]]; then
        kill "$sleep_pid" 2>/dev/null || true
    fi
    exit 0
}
trap cleanup_and_exit TERM INT

meminfo_field() {
    # $1 = field name as it appears at the start of its /proc/meminfo line (no trailing colon).
    awk -v f="$1:" '$1 == f { print $2; exit }' /proc/meminfo
}

vmstat_field() {
    awk -v f="$1" '$1 == f { print $2; exit }' /proc/vmstat
}

status_field() {
    # $1 = status file, $2 = field name (with trailing colon, e.g. "VmRSS:")
    awk -v f="$2" '$1 == f { print $2; exit }' "$1" 2>/dev/null
}

while true; do
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    mem_total="$(meminfo_field MemTotal)"
    mem_free="$(meminfo_field MemFree)"
    mem_available="$(meminfo_field MemAvailable)"
    cached="$(meminfo_field Cached)"
    mapped="$(meminfo_field Mapped)"
    swap_free="$(meminfo_field SwapFree)"
    pswpin="$(vmstat_field pswpin)"
    pswpout="$(vmstat_field pswpout)"

    llama_lines=()
    for pid_dir in /proc/[0-9]*; do
        pid="${pid_dir#/proc/}"
        comm="$(cat "$pid_dir/comm" 2>/dev/null)" || continue
        [[ "$comm" == "llama-server" ]] || continue
        status_path="$pid_dir/status"
        [[ -r "$status_path" ]] || continue
        vmrss="$(status_field "$status_path" "VmRSS:")" || continue
        rssfile="$(status_field "$status_path" "RssFile:")" || continue
        rssanon="$(status_field "$status_path" "RssAnon:")" || continue
        vmswap="$(status_field "$status_path" "VmSwap:")" || continue
        if [[ -z "$vmrss" || -z "$rssfile" || -z "$rssanon" || -z "$vmswap" ]]; then
            continue  # exited between the comm read and the status read -- skip, not an error
        fi
        llama_lines+=("pid=$pid VmRSS=$vmrss RssFile=$rssfile RssAnon=$rssanon VmSwap=$vmswap")
    done

    line="$ts MemTotal=$mem_total MemFree=$mem_free MemAvailable=$mem_available Cached=$cached Mapped=$mapped SwapFree=$swap_free pswpin=$pswpin pswpout=$pswpout llama-server=${#llama_lines[@]}"
    for extra in "${llama_lines[@]}"; do
        line="$line $extra"
    done
    printf '%s\n' "$line" >> "$out_path"

    sleep "$interval" &
    sleep_pid=$!
    wait "$sleep_pid"
    sleep_pid=""
done
