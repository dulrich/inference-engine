#!/usr/bin/env bash
# Render and install the router-slots-poller systemd --user unit.
# Never enables or starts anything: the user runs the printed line.
# Shape and substitution convention follow sidecar/install.sh (read-only as a pattern).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
POLLER_JS="$SCRIPT_DIR/router-slots-poller.mjs"
TEMPLATE="$SCRIPT_DIR/router-slots-poller.service.in"
UNIT_NAME="router-slots-poller.service"

ACCEPT_RESIDENCY=0
INSTALL_HOME="${POLLER_INSTALL_HOME:-$HOME}"
BASE_URL="${POLLER_BASE_URL:-http://127.0.0.1:4811}"
OUT_DIR=""

usage() {
  cat <<'USAGE'
usage: install-router-slots-poller.sh --i-accept-model-residency [--home <dir>] [--base-url <url>] [--out-dir <dir>]

  --i-accept-model-residency
                    REQUIRED. Without it the installer refuses and installs nothing.
                    The poller is disabled by user decision 2026-10-19: polling
                    /upstream/<model>/slots mutates model residency (see the refusal text).
  --home <dir>      Install under <dir> instead of $HOME (also POLLER_INSTALL_HOME).
                    With a non-default home, `systemctl --user daemon-reload` is
                    skipped (test/dry mode); nothing is ever enabled or started.
  --base-url <url>  llama-swap base URL baked into the unit (also POLLER_BASE_URL).
                    Default http://127.0.0.1:4811.
  --out-dir <dir>   jsonl output directory baked into the unit. Default
                    <home>/.local/state/inference-router/logs -- the SAME directory the
                    router's telemetry sink writes to, because the dashboard mounts that
                    one directory read-only and reads both feeds from it.
USAGE
}

die() { printf 'install-router-slots-poller.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --home)
      [ $# -ge 2 ] || die "--home requires a directory argument"
      INSTALL_HOME="$2"; shift 2 ;;
    --home=*)     INSTALL_HOME="${1#--home=}"; shift ;;
    --base-url)
      [ $# -ge 2 ] || die "--base-url requires a URL argument"
      BASE_URL="$2"; shift 2 ;;
    --base-url=*) BASE_URL="${1#--base-url=}"; shift ;;
    --out-dir)
      [ $# -ge 2 ] || die "--out-dir requires a directory argument"
      OUT_DIR="$2"; shift 2 ;;
    --out-dir=*)  OUT_DIR="${1#--out-dir=}"; shift ;;
    --i-accept-model-residency) ACCEPT_RESIDENCY=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

if [ "$ACCEPT_RESIDENCY" -ne 1 ]; then
  cat >&2 <<'REFUSE'
install-router-slots-poller.sh: refusing to install (nothing was written).

The slots poller was disabled by user decision 2026-10-19 because polling /upstream/<model>/slots mutates model residency, two measured ways:
  (a) An /upstream/<model>/slots request counts as activity, so a polled model never reaches its ttl (the 27B stayed resident ~10 h past its ttl of 1800 on 2026-10-19).
  (b) An /upstream hit on an exclusive-group model evicts the other groups' members, so each tick evicted bge-m3/bge-small and the next polls cold-started them (1.25 s each, ~10k spawns in 72 h).

Re-enabling needs a design that never touches /upstream of resident exclusive-group models (dashboard/AGENTS.md, Router status plane).
To install anyway, pass --i-accept-model-residency.
REFUSE
  exit 1
fi

[ -n "$INSTALL_HOME" ] || die "install home resolves to an empty path"
[ -n "$BASE_URL" ]     || die "--base-url resolves to an empty value"
[ -f "$TEMPLATE" ]     || die "missing unit template: $TEMPLATE"
[ -f "$POLLER_JS" ]    || die "missing poller entrypoint: $POLLER_JS"

[ -n "$OUT_DIR" ] || OUT_DIR="$INSTALL_HOME/.local/state/inference-router/logs"

REAL_HOME=0
if [ "$INSTALL_HOME" = "${HOME:-}" ]; then
  REAL_HOME=1
fi

# --- resolve bun by absolute path --------------------------------------------
# The systemd --user PATH on this host does not contain the bun install dir, so
# the interpreter must be baked in absolutely (sidecar/install.sh does the same
# for node).
BUN_BIN="$(command -v bun || true)"
[ -n "$BUN_BIN" ] || die "bun not found on PATH"
case "$BUN_BIN" in /*) ;; *) die "bun resolved to a non-absolute path: $BUN_BIN" ;; esac
BUN_DIR="$(dirname -- "$BUN_BIN")"
UNIT_PATH_VALUE="$BUN_DIR:/usr/local/bin:/usr/bin:/bin"

# --- render the unit ----------------------------------------------------------
UNIT_DIR="$INSTALL_HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"

rendered="$(sed \
  -e "s|@BUN@|$BUN_BIN|g" \
  -e "s|@POLLER@|$POLLER_JS|g" \
  -e "s|@BASE_URL@|$BASE_URL|g" \
  -e "s|@OUT_DIR@|$OUT_DIR|g" \
  -e "s|@PATH@|$UNIT_PATH_VALUE|g" \
  -- "$TEMPLATE")"

mkdir -p -- "$UNIT_DIR"
if [ -f "$UNIT_FILE" ] && [ "$rendered" = "$(cat -- "$UNIT_FILE")" ]; then
  unit_state="unchanged"
else
  if [ -f "$UNIT_FILE" ]; then unit_state="updated"; else unit_state="created"; fi
  printf '%s\n' "$rendered" > "$UNIT_FILE"
  chmod 0644 -- "$UNIT_FILE"
fi
printf 'unit    %s (%s)\n' "$UNIT_FILE" "$unit_state"
printf 'out-dir %s\n' "$OUT_DIR"

# --- daemon-reload (real home only) ------------------------------------------
if [ "$REAL_HOME" -eq 1 ]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    printf 'reload  systemctl --user daemon-reload: done\n'
  else
    printf 'reload  skipped: systemctl not found\n'
  fi
else
  printf 'reload  skipped: --home %s is not the current HOME (test render)\n' "$INSTALL_HOME"
fi

# --- next steps for the user --------------------------------------------------
printf '\nNext steps (run these yourself; this script never enables or starts anything):\n'
printf '  systemctl --user enable --now router-slots-poller\n'
# SC2016: the $(date …) is deliberately literal — it is a line for the user to paste.
# shellcheck disable=SC2016
printf '  tail -n1 %s/slots-$(date -u +%%F).jsonl\n' "$OUT_DIR"
