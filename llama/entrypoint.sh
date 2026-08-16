#!/bin/sh
# Refuse to start on the CPU.
#
# The failure this guards against is not a crash, it is a success that is quietly worthless. A
# Vulkan loader with no usable GPU driver still enumerates `llvmpipe`, a software rasteriser, as
# a perfectly valid Vulkan device. llama-server will happily run on it, answer every request,
# pass any "is the port open" healthcheck, and be several times *slower* than the CPU containers
# this service replaced — with nothing anywhere saying so. Measured on this box the gap is 6x for
# embedding and 24x for reranking, so a silent fallback is not a degraded service, it is a
# regression wearing the costume of a working one.
#
# The same shape bit us a second time from a different direction (2026-08), so this script now
# guards two things rather than one. A multi-stage image on a rolling base can skew its own
# glibc: the binaries get built against a newer libc than the runtime stage carries, and then
# every llama-server invocation dies in the dynamic loader before main() runs. Because the
# router (llama-swap) is a static Go binary it survives that and keeps answering /health with
# OK, so the container reports (healthy) for as long as you leave it while returning 500 to
# every actual request. See the Dockerfile's runtime stage for the fix that prevents it.
#
# So: prove a hardware device is present, prove the inference binary can actually run, print
# what it resolves, and only then exec. Set CRUCIBLE_ALLOW_CPU_VULKAN=1 to bypass the *device*
# assertion, which exists for debugging on a machine with no GPU and should never be set in the
# fleet. There is deliberately no bypass for the ABI assertion — a link-broken image has no
# working configuration to debug into.
set -eu

echo "[entrypoint] vulkan devices visible to this container:"
vulkaninfo --summary 2>/dev/null | grep -E 'deviceName|deviceType|driverName|driverInfo' || {
	echo "[entrypoint] FATAL: vulkaninfo produced no device information at all." >&2
	echo "[entrypoint] The container almost certainly has no /dev/dri passthrough. Check that" >&2
	echo "[entrypoint] the compose service has devices: [\"/dev/dri:/dev/dri\"] and group_add for" >&2
	echo "[entrypoint] the host's render and video groups." >&2
	exit 1
}

if [ "${CRUCIBLE_ALLOW_CPU_VULKAN:-0}" != "1" ]; then
	# INTEGRATED is accepted as well as DISCRETE: an APU is a legitimate deployment, llvmpipe is
	# not. The test is the device *type*, not the driver name, because a future driver name is
	# not knowable here while "is this silicon" is.
	if ! vulkaninfo --summary 2>/dev/null | grep -qE 'PHYSICAL_DEVICE_TYPE_(DISCRETE|INTEGRATED)_GPU'; then
		echo "[entrypoint] FATAL: no hardware Vulkan device found — only a software rasteriser." >&2
		echo "[entrypoint] Running here would be slower than the CPU services this replaces, so" >&2
		echo "[entrypoint] the container refuses to start rather than silently degrade." >&2
		echo "[entrypoint] Most likely cause: the image's Mesa is too old for this GPU. This box" >&2
		echo "[entrypoint] needs Mesa >= 26 for gfx1201 (RDNA4); see the Dockerfile's header." >&2
		echo "[entrypoint] Override with CRUCIBLE_ALLOW_CPU_VULKAN=1 only for debugging." >&2
		exit 1
	fi
fi

# What vulkaninfo sees and what ggml resolves are two different questions, and only the second
# one decides where the tensors land. Print it so `docker logs` answers "did it use the GPU?"
# without anyone having to reproduce a benchmark.
#
# This doubles as the ABI assertion, and it is fail-closed on purpose: it is the cheapest place
# in the whole stack to discover that llama-server cannot dynamically link, because it runs once
# at boot instead of once per spawned child, and a container that refuses to start is visible in
# a way that a (healthy) container serving 500s is not.
#
# Capture into a variable and *then* test, rather than piping straight into sed: a pipeline
# reports the exit status of its last command, so `llama-server ... | sed` is 0 even when
# llama-server exits 1. That — not the `|| true` this replaces — is what silently swallowed the
# loader failure during the 2026-08 outage. Verified against the broken image: the bare command
# exits 1, the piped form exits 0.
echo "[entrypoint] devices as ggml resolves them:"
if ! ggml_devices="$(llama-server --list-devices 2>&1)"; then
	echo "$ggml_devices" | sed 's/^/[entrypoint]   /' >&2
	echo "[entrypoint] FATAL: llama-server could not enumerate compute devices." >&2
	echo "[entrypoint] If the output above reports GLIBC_ symbol versions not found, this image" >&2
	echo "[entrypoint] was built against a newer glibc than its runtime stage carries. The base" >&2
	echo "[entrypoint] image (debian:sid-slim) rolls forward, and the build stage's toolchain" >&2
	echo "[entrypoint] upgrades libc6 as a side effect while the runtime stage does not. The" >&2
	echo "[entrypoint] Dockerfile pins the runtime libc6 to the version the builder resolved;" >&2
	echo "[entrypoint] compare /usr/local/share/llama-abi/libc6.version against the installed" >&2
	echo "[entrypoint] libc6 and rebuild with --no-cache if they disagree." >&2
	echo "[entrypoint] Refusing to start. A link-broken image is worse than a stopped one: the" >&2
	echo "[entrypoint] router would answer /health with OK while failing every real request." >&2
	exit 1
fi
echo "$ggml_devices" | sed 's/^/[entrypoint]   /'

# Dual-use tail: this used to hardcode `exec llama-server "$@"`, but the image now also runs
# llama-swap (which spawns its own llama-server children internally per its config.yaml) as the
# default CMD. Both assertions above are identical either way — they belong to the container,
# not to whichever process ends up owning the tensors — so only this exec needs to become
# argv-agnostic. Passing a bare `llama-server -m ... --embeddings ...` command still works
# exactly as before, and gets the ABI check for free: the check runs the same binary that
# command would exec, so the bare path cannot start link-broken either.
echo "[entrypoint] starting: $*"
exec "$@"
