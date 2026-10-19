#!/usr/bin/env bash
# Install the codex-sidecar systemd --user unit, config dir and env file.
# Never enables or starts anything: the user runs the printed lines.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/codex-sidecar.service.in"
UNIT_NAME="codex-sidecar.service"
PERSONAS=(codex-a codex-b)

INSTALL_HOME="${SIDECAR_INSTALL_HOME:-$HOME}"
RUST_BIN="${SIDECAR_RUST_BIN:-}"

# fleet-env cutover switch (plans/env-management.md). 0 = ON, 1 = OFF; exits loudly if
# ON but the fleet-env binary is missing or the switch is malformed (never a silent
# fallback to the legacy file).
FLEET_ENV="${FLEET_ENV_BIN:-<code-root>/fleet-tools/fleet/fleet_env/bin/fleet-env}"
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
  "$FLEET_ENV" enabled || rc=$?
  if [[ $rc -gt 1 ]]; then
    printf '%s: fleet-env enabled failed (exit %d)\n' "$0" "$rc" >&2
    exit 1
  fi
  return "$rc"
}

# Cutover ON: the unit reads the tmpfs projection %t/fleet-env/codex-sidecar.env, ordered
# after fleet-env-project.service; the key lives only in the canonical store (no legacy env
# file is created or touched, no key is ever printed). OFF (default): today's behaviour.
FLEET_ENV_MODE=0
if fleet_env_on; then FLEET_ENV_MODE=1; fi

usage() {
  cat <<'USAGE'
usage: install.sh [--home <dir>] [--rust-bin <path>]

  Builds rs/ in release from this checkout and installs the binary atomically to
  <home>/.local/lib/codex-sidecar/codex-sidecar (the previous build kept as
  codex-sidecar.prev), then renders the unit to run it.

  --home <dir>      Install under <dir> instead of $HOME (also SIDECAR_INSTALL_HOME).
                    With a non-default home, `systemctl --user daemon-reload` is
                    skipped (test/dry mode); nothing is ever enabled or started.
  --rust-bin <path> Install this prebuilt binary instead of building
                    (also SIDECAR_RUST_BIN; tests use a stub).
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
    --rust-bin)
      [ $# -ge 2 ] || die "--rust-bin requires a path argument"
      RUST_BIN="$2"
      shift 2
      ;;
    --rust-bin=*)
      RUST_BIN="${1#--rust-bin=}"
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[ -n "$INSTALL_HOME" ] || die "install home resolves to an empty path"
[ -f "$TEMPLATE" ]   || die "missing unit template: $TEMPLATE"
[ -z "$RUST_BIN" ] || [ -x "$RUST_BIN" ] || die "--rust-bin is not an executable file: $RUST_BIN"

REAL_HOME=0
if [ "$INSTALL_HOME" = "${HOME:-}" ]; then
  REAL_HOME=1
fi

# --- resolve the executables the unit needs by absolute path ------------------
# The systemd --user PATH on this host does not contain ~/.local/sbin, so the persona
# wrapper dirs must be baked in absolutely.
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

UNIT_PATH_VALUE="$WRAPPER_DIR:/usr/local/bin:/usr/bin:/bin"

# --- build (or take the prebuilt) and install atomically ---------------
# The installed binary reads preamble.txt + thread-config.json from the checkout it was
# built from (rs/src/config.rs pinned_dir: CARGO_MANIFEST_DIR/..), so the build always
# runs against THIS script's directory, and the pinned files must exist here.
for f in preamble.txt thread-config.json; do
  [ -f "$SCRIPT_DIR/$f" ] || die "missing pinned file $SCRIPT_DIR/$f (the rust binary reads it at runtime)"
done
src_bin="$RUST_BIN"
if [ -z "$src_bin" ]; then
  command -v cargo >/dev/null 2>&1 || die "cargo not found; needed to build the sidecar"
  cargo build --release --manifest-path "$SCRIPT_DIR/rs/Cargo.toml" >&2 \
    || die "cargo build --release --manifest-path $SCRIPT_DIR/rs/Cargo.toml failed"
  src_bin="$SCRIPT_DIR/rs/target/release/codex-sidecar"
  printf 'build   cargo build --release --manifest-path %s/rs/Cargo.toml (pinned files: %s)\n' "$SCRIPT_DIR" "$SCRIPT_DIR"
else
  printf 'build   skipped: prebuilt %s (pinned files must resolve to its build checkout)\n' "$src_bin"
fi
[ -x "$src_bin" ] || die "rust binary not found or not executable: $src_bin"
LIB_DIR="$INSTALL_HOME/.local/lib/codex-sidecar"
BIN_DEST="$LIB_DIR/codex-sidecar"
mkdir -p -- "$LIB_DIR"
tmp_bin="$(mktemp -- "$LIB_DIR/.codex-sidecar.XXXXXX")"
cp -- "$src_bin" "$tmp_bin"
chmod 0755 -- "$tmp_bin"
if [ -f "$BIN_DEST" ]; then
  cp -p -- "$BIN_DEST" "$LIB_DIR/codex-sidecar.prev"
fi
mv -f -- "$tmp_bin" "$BIN_DEST"
printf 'binary  %s (installed; previous kept as codex-sidecar.prev when present)\n' "$BIN_DEST"
EXEC_VALUE="$BIN_DEST"

# --- render the unit ----------------------------------------------------------
UNIT_DIR="$INSTALL_HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/$UNIT_NAME"
CONF_DIR="$INSTALL_HOME/.config/codex-sidecar"
ENV_FILE="$CONF_DIR/env"

# @FLEET_ENV_UNIT@ is the template's sole ordering exception (see its header): rendered as
# After=/Requires=fleet-env-project.service under the cutover, deleted otherwise so the
# switch-off unit is byte-identical to the pre-cutover render.
if [ "$FLEET_ENV_MODE" -eq 1 ]; then
  UNIT_ENV_FILE='%t/fleet-env/codex-sidecar.env'
  unit_deps=(-e 's|^@FLEET_ENV_UNIT@$|After=fleet-env-project.service\nRequires=fleet-env-project.service|')
else
  UNIT_ENV_FILE='%h/.config/codex-sidecar/env'
  unit_deps=(-e '/^@FLEET_ENV_UNIT@$/d')
fi

rendered="$(sed \
  "${unit_deps[@]}" \
  -e "s|@ENV_FILE@|$UNIT_ENV_FILE|g" \
  -e "s|@EXEC@|$EXEC_VALUE|g" \
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
generated_key=""
if [ "$FLEET_ENV_MODE" -eq 1 ]; then
  # The key is read only through the canonical store (`fleet-env run sidecar-install`), and
  # only tested for presence -- never printed, never written to a file here.
  # shellcheck disable=SC2016  # expanded by the child shell, not here
  "$FLEET_ENV" run sidecar-install -- bash -c '[ -n "${CODEX_SIDECAR_KEY:-}" ]' \
    || die "fleet-env cutover is on but CODEX_SIDECAR_KEY is not available from the store (fleet-env check)"
  projection="$("$FLEET_ENV" path codex-sidecar)" || die "fleet-env path codex-sidecar failed"
  [ -f "$projection" ] || die "fleet-env cutover is on but $projection is missing; run fleet-env project (or install fleet-env-project.service)"
  printf 'env     %s (fleet-env projection; CODEX_SIDECAR_KEY from the canonical store, legacy %s not used)\n' "$projection" "$ENV_FILE"
else
mkdir -p -- "$CONF_DIR"
chmod 0700 -- "$CONF_DIR"

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
