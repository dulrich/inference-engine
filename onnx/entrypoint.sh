#!/usr/bin/env bash
# inference-engine-onnx entrypoint: prove the image is what its name says, then exec.
#
# Modelled on llama/entrypoint.sh, which refuses to start a "GPU" server that silently resolved
# llvmpipe. The equivalent lie here is a container called `-onnx` that is actually running torch,
# or an onnxruntime that resolved no execution provider at all. Both would pass any
# port-is-open healthcheck and answer requests, so they are asserted here instead — before the
# listener exists, where the failure is loud and attributable.
#
# Everything after the assertions is exec'd unchanged, so `docker run ... python -c ...` still
# works for debugging.

# A leading --help/-h answers with usage before any GPU/model check; later arguments belong to
# the command this entrypoint execs.
case "${1:-}" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '[command [args...]]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac

set -euo pipefail

log() { echo "[entrypoint] $*"; }

# ── 1. There must be no torch in this image ─────────────────────────────────────────────────
# The exporter (tools/export_gliner2.py) needs torch; the server must not. If a future edit adds
# a dependency that drags torch in, the image doubles in size and the container stops being what
# its name claims — catch that at start-up, not in a size review six months later.
if python -c "import torch" 2>/dev/null; then
	log "FATAL: torch is importable inside inference-engine-onnx."
	log "       This image serves ONNX graphs through onnxruntime and must not carry torch."
	log "       Something added a dependency that pulls it in — fix the Dockerfile, not this check."
	exit 1
fi
log "torch is absent (as it must be)"

# ── 2. onnxruntime must be present AND have resolved a usable provider ──────────────────────
# `import onnxruntime` succeeding says nothing about whether it can execute anything.
ORT_INFO="$(python - <<'PY'
import sys
try:
    import onnxruntime as ort
except Exception as exc:
    print(f"IMPORT_FAILED {type(exc).__name__}: {exc}")
    sys.exit(0)
providers = ort.get_available_providers()
print(f"{ort.__version__} {','.join(providers) or 'NONE'}")
PY
)"
case "$ORT_INFO" in
IMPORT_FAILED*)
	log "FATAL: onnxruntime did not import: ${ORT_INFO#IMPORT_FAILED }"
	exit 1
	;;
esac
ORT_VERSION="${ORT_INFO%% *}"
ORT_PROVIDERS="${ORT_INFO#* }"
if ! echo "$ORT_PROVIDERS" | grep -q "CPUExecutionProvider"; then
	log "FATAL: onnxruntime $ORT_VERSION resolved no CPU execution provider (providers: $ORT_PROVIDERS)"
	exit 1
fi
log "onnxruntime $ORT_VERSION, providers: $ORT_PROVIDERS"

# ── 3. The model registry has to exist; the models it names do not have to (yet) ────────────
# A missing model is reported per-alias on /health and is recoverable by fetching weights. A
# missing registry means the compose bind-mount is wrong, and Docker's answer to a stale mount
# source is an empty directory rather than an error (see the root AGENTS.md quirk) — so an
# unreadable registry is worth failing on rather than serving zero models and looking healthy.
CONFIG_PATH="${ONNX_MODELS_CONFIG:-/app/models.yaml}"
if [ ! -f "$CONFIG_PATH" ]; then
	log "FATAL: model registry not found at $CONFIG_PATH"
	log "       compose bind-mounts onnx/models.yaml there; a stale mount source silently"
	log "       becomes an empty directory, so check the path in compose.fleet.yml first."
	exit 1
fi
log "model registry: $CONFIG_PATH"

if [ ! -d /models ]; then
	log "WARNING: /models is not mounted — every configured model will report loaded:false"
fi

exec "$@"
