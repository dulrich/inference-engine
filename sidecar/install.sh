#!/usr/bin/env bash
# Install the codex-sidecar systemd --user unit (credentials come from the fleet-env store).
# Never enables or starts anything: the user runs the printed lines.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/codex-sidecar.service.in"
UNIT_NAME="codex-sidecar.service"
PERSONAS=(codex-a codex-b)

INSTALL_HOME="${SIDECAR_INSTALL_HOME:-$HOME}"
RUST_BIN="${SIDECAR_RUST_BIN:-}"

# fleet-env store (plans/env-management.md): the unit reads the tmpfs projection
# %t/fleet-env/codex-sidecar.env, ordered after fleet-env-project.service. The key lives only
# in the canonical store (no env file is created or touched, no key is ever printed). A missing
# fleet-env binary or projection fails loudly -- never a silent fallback.
FLEET_ENV="${FLEET_ENV_BIN:-<code-root>/fleet-tools/fleet/fleet_env/bin/fleet-env}"

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

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../scripts/lib/build-id.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

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
[ -x "$FLEET_ENV" ] || die "fleet-env binary $FLEET_ENV is missing; refusing to install without the credential store"

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

# @FLEET_ENV_UNIT@ is the template's sole ordering exception (see its header): rendered as
# After=/Requires=fleet-env-project.service.
UNIT_ENV_FILE='%t/fleet-env/codex-sidecar.env'
unit_deps=(-e 's|^@FLEET_ENV_UNIT@$|After=fleet-env-project.service\nRequires=fleet-env-project.service|')

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

# --- credentials: the fleet-env store -----------------------------------------
# The key is read only through the canonical store (`fleet-env run sidecar-install`), and
# only tested for presence -- never printed, never written to a file here.
# shellcheck disable=SC2016  # expanded by the child shell, not here
"$FLEET_ENV" run sidecar-install -- bash -c '[ -n "${CODEX_SIDECAR_KEY:-}" ]' \
  || die "CODEX_SIDECAR_KEY is not available from the fleet-env store (fleet-env check)"
projection="$("$FLEET_ENV" path codex-sidecar)" || die "fleet-env path codex-sidecar failed"
[ -f "$projection" ] || die "$projection is missing; run fleet-env project (or install fleet-env-project.service)"
printf 'env     %s (fleet-env projection; CODEX_SIDECAR_KEY from the canonical store)\n' "$projection"

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

# --- next steps for the user --------------------------------------------------
printf '\nNext steps (run these yourself; install.sh never enables or starts anything):\n'
if [ "$REAL_HOME" -eq 1 ] && [ "$linger" != "yes" ]; then
  printf '  loginctl enable-linger %s\n' "${USER:-$(id -un)}"
fi
if [ "$REAL_HOME" -eq 1 ] && command -v systemctl >/dev/null 2>&1 &&
  [ "$(systemctl --user is-enabled codex-sidecar.service 2>/dev/null)" = "enabled" ]; then
  # upgrade: the unit is already enabled and the daemon-reload above already ran,
  # so the new binary only needs a restart (a test home never touches systemd)
  printf '  systemctl --user restart codex-sidecar.service\n'
else
  printf '  systemctl --user enable --now codex-sidecar\n'
fi
printf '  %s/status.sh\n' "$SCRIPT_DIR"
