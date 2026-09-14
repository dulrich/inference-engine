#!/usr/bin/env bash
# Install the codex-sidecar systemd --user unit, config dir and env file.
# Never enables or starts anything: the user runs the printed lines.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SIDECAR_JS="$SCRIPT_DIR/codex-sidecar.mjs"
TEMPLATE="$SCRIPT_DIR/codex-sidecar.service.in"
UNIT_NAME="codex-sidecar.service"
MIN_NODE_MAJOR=24
PERSONAS=(codex-a codex-b)

INSTALL_HOME="${SIDECAR_INSTALL_HOME:-$HOME}"

usage() {
  cat <<'USAGE'
usage: install.sh [--home <dir>]

  --home <dir>   Install under <dir> instead of $HOME (also SIDECAR_INSTALL_HOME).
                 With a non-default home, `systemctl --user daemon-reload` is
                 skipped (test/dry mode); nothing is ever enabled or started.
USAGE
}

die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --home)
      [ $# -ge 2 ] || die "--home requires a directory argument"
      INSTALL_HOME="$2"
      shift 2
      ;;
    --home=*)
      INSTALL_HOME="${1#--home=}"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[ -n "$INSTALL_HOME" ] || die "install home resolves to an empty path"
[ -f "$TEMPLATE" ]   || die "missing unit template: $TEMPLATE"
[ -f "$SIDECAR_JS" ] || die "missing sidecar entrypoint: $SIDECAR_JS"

REAL_HOME=0
if [ "$INSTALL_HOME" = "${HOME:-}" ]; then
  REAL_HOME=1
fi

# --- resolve the executables the unit needs by absolute path ------------------
# The systemd --user PATH on this host contains neither the nvm node dir nor
# ~/.local/sbin, so both must be baked in absolutely.
# Prefer the version-stable ~/.nvm/current link (maintained by nvm under
# NVM_SYMLINK_CURRENT=true, see the tracked bashrc) so the rendered unit survives
# a node bump without a re-render; fall back to whatever node is on PATH.
NODE_BIN="$INSTALL_HOME/.nvm/current/bin/node"
[ -x "$NODE_BIN" ] || NODE_BIN="$(command -v node || true)"
[ -n "$NODE_BIN" ] || die "node not found (no $INSTALL_HOME/.nvm/current/bin/node and none on PATH); need node >= ${MIN_NODE_MAJOR}"
case "$NODE_BIN" in /*) ;; *) die "node resolved to a non-absolute path: $NODE_BIN" ;; esac

NODE_VERSION="$("$NODE_BIN" --version)"          # e.g. v24.15.0
NODE_MAJOR="${NODE_VERSION#v}"
NODE_MAJOR="${NODE_MAJOR%%.*}"
case "$NODE_MAJOR" in
  ''|*[!0-9]*) die "cannot parse node version from '$NODE_VERSION'" ;;
esac
[ "$NODE_MAJOR" -ge "$MIN_NODE_MAJOR" ] \
  || die "node $NODE_VERSION is too old; need >= ${MIN_NODE_MAJOR} (found at $NODE_BIN)"

WRAPPER_DIR=""
for persona in "${PERSONAS[@]}"; do
  wrapper="$(command -v "$persona" || true)"
  [ -n "$wrapper" ] || die "persona wrapper '$persona' not found on PATH"
  case "$wrapper" in /*) ;; *) die "wrapper '$persona' resolved to a non-absolute path: $wrapper" ;; esac
  # PATH-visible dir, not the symlink target: the unit needs the name the
  # sidecar spawns ("<persona> app-server") to be findable on the unit PATH.
  dir="$(dirname -- "$wrapper")"
  if [ -z "$WRAPPER_DIR" ]; then
    WRAPPER_DIR="$dir"
  elif [ "$WRAPPER_DIR" != "$dir" ]; then
    WRAPPER_DIR="$WRAPPER_DIR:$dir"
  fi
done

NODE_DIR="$(dirname -- "$NODE_BIN")"
UNIT_PATH_VALUE="$NODE_DIR:$WRAPPER_DIR:/usr/local/bin:/usr/bin:/bin"

# --- render the unit ----------------------------------------------------------
UNIT_DIR="$INSTALL_HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"
CONF_DIR="$INSTALL_HOME/.config/codex-sidecar"
ENV_FILE="$CONF_DIR/env"

rendered="$(sed \
  -e "s|@NODE@|$NODE_BIN|g" \
  -e "s|@SIDECAR@|$SIDECAR_JS|g" \
  -e "s|@PATH@|$UNIT_PATH_VALUE|g" \
  -- "$TEMPLATE")"

mkdir -p -- "$UNIT_DIR"
if [ -f "$UNIT_FILE" ] && [ "$rendered" = "$(cat -- "$UNIT_FILE")" ]; then
  unit_state="unchanged"
else
  if [ -f "$UNIT_FILE" ]; then
    unit_state="updated"
  else
    unit_state="created"
  fi
  printf '%s\n' "$rendered" > "$UNIT_FILE"
  chmod 0644 -- "$UNIT_FILE"
fi
printf 'unit    %s (%s)\n' "$UNIT_FILE" "$unit_state"

# --- config dir + env file ----------------------------------------------------
mkdir -p -- "$CONF_DIR"
chmod 0700 -- "$CONF_DIR"

generated_key=""
if [ -f "$ENV_FILE" ]; then
  chmod 0600 -- "$ENV_FILE"
  printf 'env     %s (unchanged; existing SIDECAR_KEY kept)\n' "$ENV_FILE"
else
  if command -v openssl >/dev/null 2>&1; then
    generated_key="$(openssl rand -hex 32)"
  else
    generated_key="$(od -vAn -N32 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  (umask 077; printf 'SIDECAR_KEY=%s\n' "$generated_key" > "$ENV_FILE")
  chmod 0600 -- "$ENV_FILE"
  printf 'env     %s (created; SIDECAR_KEY generated)\n' "$ENV_FILE"
fi

# --- daemon-reload (real home only) ------------------------------------------
if [ "$REAL_HOME" -eq 1 ]; then
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
    printf 'reload  systemctl --user daemon-reload: done\n'
  else
    printf 'reload  skipped: systemctl not found\n'
  fi
else
  printf 'reload  skipped: --home %s is not the current HOME (test render; run daemon-reload yourself if that is a real home)\n' "$INSTALL_HOME"
fi

# --- linger check -------------------------------------------------------------
linger=""
if [ "$REAL_HOME" -eq 1 ] && command -v loginctl >/dev/null 2>&1; then
  linger="$(loginctl show-user "${USER:-$(id -un)}" -p Linger --value 2>/dev/null || true)"
  if [ "$linger" = "yes" ]; then
    printf 'linger  enabled (unit will start at boot)\n'
  else
    printf 'linger  NOT enabled\n'
  fi
else
  printf 'linger  not checked (test render)\n'
fi

# --- one-time key line --------------------------------------------------------
if [ -n "$generated_key" ]; then
  printf '\n'
  printf 'Generated SIDECAR_KEY (shown once, stored 0600 in %s):\n' "$ENV_FILE"
  printf '  %s\n' "$generated_key"
  printf 'Copy this into the fleet-tools .env file as CODEX_SIDECAR_KEY (WP-3 wires the router to it).\n'
fi

# --- next steps for the user --------------------------------------------------
printf '\nNext steps (run these yourself; install.sh never enables or starts anything):\n'
if [ "$REAL_HOME" -eq 1 ] && [ "$linger" != "yes" ]; then
  printf '  loginctl enable-linger %s\n' "${USER:-$(id -un)}"
fi
printf '  systemctl --user enable --now codex-sidecar\n'
printf '  %s/status.sh\n' "$SCRIPT_DIR"
