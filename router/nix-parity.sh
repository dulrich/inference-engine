#!/usr/bin/env bash
# router/nix-parity.sh — parity gate between router/Dockerfile (the deployed build) and
# router/flake.nix (the Nix build). Fails on any of:
#   (a) upstream pin or ORDERED patch list differs between the two;
#   (b) the Nix build fails, or its build-stage vitest count differs from the Dockerfile build's;
#   (c) the Nix-built server does not answer /health 200 on a scratch 127.0.0.1 port
#       with an empty-provider config.
# Usage: router/nix-parity.sh [--equivalence-only]   (from anywhere; operates on its own dir)
# Docker build uses the scratch tag nix-wp3/router-parity:tmp and removes it afterwards.
# Never touches the live router container or :4813.
set -euo pipefail

ROUTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG="nix-wp3/router-parity:tmp"
WORK="$(mktemp -d)"
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]]; then kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; fi
  docker rmi -f "$TAG" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT
fail() { echo "PARITY FAIL: $*" >&2; exit 1; }

# ---- (a) upstream pin + ordered patch list ------------------------------------------------
docker_rev="$(sed -n 's/^ARG AI_GATEWAY_COMMIT=\([0-9a-f]\{40\}\)$/\1/p' "$ROUTER_DIR/Dockerfile")"
[[ -n "$docker_rev" ]] || fail "no AI_GATEWAY_COMMIT pin found in Dockerfile"
docker_patches="$(sed -n 's|^RUN git apply --check /tmp/\([^ ]*\.patch\).*$|\1|p' "$ROUTER_DIR/Dockerfile")"
[[ -n "$docker_patches" ]] || fail "no git apply steps found in Dockerfile"

nix_rev="$(nix eval --raw "path:$ROUTER_DIR#router.src.rev" 2>/dev/null)"
nix_patches="$(nix eval --raw "path:$ROUTER_DIR#router.patches" \
  --apply 'ps: builtins.concatStringsSep "\n" (map (p: baseNameOf (toString p)) ps)' 2>/dev/null)"

echo "Dockerfile pin: $docker_rev"
echo "flake pin:      $nix_rev"
[[ "$docker_rev" == "$nix_rev" ]] || fail "upstream pin differs (Dockerfile $docker_rev vs flake $nix_rev)"
if ! diff <(printf '%s\n' "$docker_patches") <(printf '%s\n' "$nix_patches"); then
  fail "ordered patch list differs (< Dockerfile, > flake)"
fi
# Every patch on disk must be applied by the Dockerfile (the flake globs patches/*.patch).
disk_patches="$(cd "$ROUTER_DIR/patches" && printf '%s\n' *.patch | sort)"
diff <(printf '%s\n' "$disk_patches") <(printf '%s\n' "$docker_patches" | sort) >/dev/null \
  || fail "patches/ on disk differ from the Dockerfile's apply list"
echo "OK (a): pin and $(printf '%s\n' "$nix_patches" | wc -l) ordered patches equivalent"
[[ "${1:-}" == "--equivalence-only" ]] && exit 0

# ---- (b) Nix build + vitest count == Dockerfile build's ------------------------------------
# vitest colours its summary; strip ANSI, take the last "Tests  N passed (M)" line, require N == M.
strip_ansi() { sed -E 's/\x1b\[[0-9;]*m//g' "$1"; }
count_tests() {
  { strip_ansi "$1" | grep -aoE 'Tests +[0-9]+ passed \([0-9]+\)' || true; } | tail -1 \
    | sed -E 's/Tests +([0-9]+) passed \(([0-9]+)\)/\1 \2/' | awk '$1==$2 {print $1}'
}
failed_tests() { strip_ansi "$1" | grep -aE 'Tests +.*[0-9]+ failed' || true; }

nix build "path:$ROUTER_DIR#router" --out-link "$WORK/result" -L >"$WORK/nix.log" 2>&1 \
  || { tail -40 "$WORK/nix.log"; fail "nix build failed"; }
# A cached build prints no log; read the stored build log instead.
nix log "path:$ROUTER_DIR#router" >>"$WORK/nix.log" 2>/dev/null || true
nix_count="$(count_tests "$WORK/nix.log")"

docker build --no-cache --progress=plain -t "$TAG" "$ROUTER_DIR" >"$WORK/docker.log" 2>&1 \
  || { tail -40 "$WORK/docker.log"; fail "docker build failed"; }
docker_count="$(count_tests "$WORK/docker.log")"

echo "nix vitest:    ${nix_count:-none} passed"
echo "docker vitest: ${docker_count:-none} passed"
[[ -z "$(failed_tests "$WORK/nix.log")$(failed_tests "$WORK/docker.log")" ]] || fail "a build log reports failing tests"
[[ -n "$nix_count" && -n "$docker_count" ]] || fail "could not parse a vitest count from a build log"
[[ "$nix_count" == "$docker_count" ]] || fail "vitest count differs (nix $nix_count vs docker $docker_count)"
echo "OK (b): both builds green at $nix_count/$nix_count"

# ---- (c) Nix-built server answers /health on a scratch loopback port ----------------------
port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
printf '{"host":"127.0.0.1","port":%s,"Providers":[]}\n' "$port" >"$WORK/gateway.config.json"
GATEWAY_CONFIG_PATH="$WORK/gateway.config.json" ROUTER_TELEMETRY_ENABLED=false \
  "$WORK/result/bin/next-ai-gateway" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
code=000
for _ in $(seq 1 50); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/health" || true)"
  [[ "$code" == 200 ]] && break
  sleep 0.2
done
[[ "$code" == 200 ]] || { cat "$WORK/server.log"; fail "/health on 127.0.0.1:$port returned $code"; }
echo "OK (c): Nix-built server /health 200 on 127.0.0.1:$port (stopped)"
echo "PARITY GREEN"
