# inference-engine-onnx — the fleet's always-resident GLiNER2-family CPU sidecar.
#
# Resulting tag: inference-engine-onnx:ort1.28-py312 (the runtime and its Python line baked into
# the tag, so the tag alone says what is inside — same convention as the llama image's
# b10121-swap243).
#
# ── The name is a claim, and this Dockerfile is where it is kept ──────────────────────────────
#
# The container is named for its runtime. That means the image must contain onnxruntime and
# NOT torch — "ships torch inside a box labelled onnx" was the explicit failure mode this work
# package was told to avoid rather than paper over. So:
#
#   * the deps below are the entire runtime: onnxruntime (CPU wheel), numpy, tokenizers, PyYAML;
#   * `entrypoint.sh` asserts at start-up that `import torch` FAILS and that onnxruntime resolved
#     a CPU provider — the same "assert the thing you claim, do not trust the healthcheck" shape
#     as llama/entrypoint.sh's Vulkan device assertion (see llama/AGENTS.md: a service that came
#     up healthy has told you nothing);
#   * the torch-side exporter (tools/export_gliner2.py) is deliberately NOT copied in. It runs on
#     a host venv, writes ONNX artifacts into the /models tree, and never ships here.
#
# ── No GPU, by design ─────────────────────────────────────────────────────────────────────────
#
# No /dev/dri, no Vulkan, no ROCm, no CUDA. This tier exists to serve the encoder+span-head
# models llama.cpp cannot, on CPU, while the GPU stays free for the llama tier. Do not add a GPU
# execution provider "for speed": the workload is 13 events/s of short paragraphs, the GPU is
# contended with an interactive desktop, and an accelerated build is a much larger image with a
# driver-version dependency this service does not otherwise have.
#
# ── Weights are mounted, never baked ──────────────────────────────────────────────────────────
#
# The exported ONNX artifacts are ~0.8-1.2 GB per model and live under the fleet's shared
# /srv/models tree (mounted read-only at /models), keyed <publisher>/<repo>-onnx, the
# same layout the llama tier uses for GGUFs. Baking them would make the image multi-GB and make
# "add a model" a rebuild instead of a config append. Consequence, and it is deliberate: this
# container needs NO network at runtime. It never calls the HF hub — a missing model surfaces as
# `loaded: false` on /health, not as a silent download.

FROM python:3.12-slim

# Source commit baked in for the fleet view (same convention as the fleet-tools images).
# Declared BEFORE the apt layer deliberately: a new commit busts the cache from here down, so
# `apt-get upgrade` re-runs and picks up base-package security fixes instead of serving a stale
# cached layer (the util-linux deb13u1 lesson, 2026-08-17). OS packages only — the python wheels
# below stay exactly pinned; this layer never touches the numerics the parity gate protects.
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.revision=${GIT_SHA}
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Pinned exactly. An inference runtime that changes numerics under a rebuild is precisely what
# the export-time parity gate exists to make impossible, and an unpinned rebuild would route
# around it. Bump deliberately, then re-run `tools/export_gliner2.py --verify-only` for every
# configured model before shipping the new image.
#   onnxruntime : the CPU wheel (there is no GPU here); ORT ships its own protobuf/flatbuffers
#   tokenizers  : the Rust fast-tokenizer, loaded straight from the exported tokenizer.json —
#                 `transformers` is NOT a dependency, which keeps a torch-adjacent tree out
#   numpy       : every decode step (span scoring, sigmoid/softmax, argmax) is numpy
#   PyYAML      : models.yaml only
ARG ONNXRUNTIME_VERSION=1.28.0
ARG NUMPY_VERSION=2.5.1
ARG TOKENIZERS_VERSION=0.22.2
ARG PYYAML_VERSION=6.0.3

RUN --mount=from=ghcr.io/astral-sh/uv:0.13.0,source=/uv,target=/usr/local/bin/uv \
    uv pip install --system --no-cache \
      "onnxruntime==${ONNXRUNTIME_VERSION}" \
      "numpy==${NUMPY_VERSION}" \
      "tokenizers==${TOKENIZERS_VERSION}" \
      "PyYAML==${PYYAML_VERSION}" \
 && uv pip uninstall --system pip

# The wheels install with uv (same pin as tts/tier/Dockerfile), bind-mounted from its image for
# this one RUN only, so the uv binary never lands in a layer. Nothing at runtime installs
# packages, so the base image's bundled pip is dead weight — and was the image's sole scan finding
# (pip 25.0.1, 5 MEDIUM + 1 LOW, 2026-09-29). uv removes it without ever running it, like
# router/Dockerfile's npm, so the final image carries neither pip nor uv.

WORKDIR /app
COPY server /app/server
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Read-only mount point for the exported ONNX model tree. Nothing in this image writes to it.
VOLUME ["/models"]

# /app/models.yaml is where compose bind-mounts the registry read-only. It is NEVER COPYed in —
# adding a model must not require a rebuild. This only guarantees the mount point exists.

# Python's stdout is a pipe under docker; unbuffered keeps `docker logs` live rather than
# arriving in 8 KiB bursts, which matters when the interesting output is a preload progress line.
#
# ONNX_INTRA_OP_THREADS=8 is a MEASURED default, not a guess, and leaving it unset is a
# 2.5x regression. onnxruntime defaults intra-op parallelism to the core count (24 on this
# host); the ops in this graph are small enough that 24 threads spend their time synchronising.
# Measured on this box, 16 crisis24-length paragraphs through gliner2-base:
#
#   intra-op threads   sequential      8-way concurrent
#   24 (ORT default)    4.8 texts/s     11.8 texts/s
#    2                  6.4 texts/s     23.8 texts/s
#    4                 10.8 texts/s     27.2 texts/s
#    8                 12.0 texts/s     25.7 texts/s
#
# 8 wins the case the launch consumer actually is (one client posting batches in sequence) and
# is within 6% of the best concurrent number. Re-measure if the host's core count changes.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ONNX_MODELS_CONFIG=/app/models.yaml \
    ONNX_HOST=0.0.0.0 \
    ONNX_PORT=4812 \
    ONNX_INTRA_OP_THREADS=8

EXPOSE 4812

RUN groupadd -g 1001 app && useradd -m -u 1001 -g app app
USER app

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "server.app"]
