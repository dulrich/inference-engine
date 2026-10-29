#!/usr/bin/env bash
# plans/small-dense-decode-roofline-spike.md WP-2 -- resumable GGUF fetcher from Hugging Face.
#
# Usage: hf-gguf-fetch.sh <org/repo> <file> [<file>...]
#
# Queries the HF tree API (`https://huggingface.co/api/models/<org>/<repo>/tree/main`) once for
# <org/repo>, resolves every named file's byte size from that listing -- erroring out before any
# download starts if a name isn't present in the listing -- then downloads each to
# <MODELS_ROOT>/<org>/<repo>/<file>.part with resumable `curl -L -C -`. On completion the `.part`
# is byte-size-compared against the API's reported size: an exact match renames `.part` to the
# final name; any mismatch refuses the rename and leaves the `.part` in place for inspection or a
# re-run (which resumes it). A complete file already on disk at the right size is skipped without
# touching the network -- the script is idempotent: a second run over the same file list is all
# `skip`. One summary line per file on stdout: `ok|skip|MISMATCH <bytes> <path>`.
#
# No `hf` CLI, no huggingface_hub, no Python deps beyond stdlib (used only as a `jq` fallback).
# MODELS_ROOT overrides the destination root (default /models) -- set it for testing.
#
# Convention: <MODELS_ROOT>/<org>/<repo>/<file>, exact HF casing (mirrors the fleet's existing
# /models/<org>/<repo>/... layout, e.g. /models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/...).

for _a in "$@"; do case "$_a" in --help|-h)
  printf 'usage: %s %s\n' "${0##*/}" '<org/repo> <file> [<file>...]'
  awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
  exit 0;; esac; done

# shellcheck source=/dev/null
. "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../../scripts/lib/build-id.sh"
build_flags "${BASH_SOURCE[0]}" "$@"

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: hf-gguf-fetch.sh <org/repo> <file> [<file>...]" >&2
    exit 1
fi

org_repo="$1"
shift

if [[ "$org_repo" != */* ]]; then
    echo "hf-gguf-fetch.sh: <org/repo> must contain a slash, got: $org_repo" >&2
    exit 1
fi

models_root="${MODELS_ROOT:-/models}"
dest_dir="$models_root/$org_repo"
mkdir -p "$dest_dir"

tree_url="https://huggingface.co/api/models/${org_repo}/tree/main"
tree_json="$(curl -sS -L --fail "$tree_url")"

# size_for_path <path> -- prints the byte size for exactly that "path" entry in the tree JSON,
# or nothing if <path> isn't present (caller checks for empty).
size_for_path() {
    local want="$1"
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$tree_json" | jq -r --arg p "$want" '.[] | select(.path == $p) | .size'
    else
        printf '%s' "$tree_json" | python3 -c '
import json, sys
want = sys.argv[1]
data = json.load(sys.stdin)
for e in data:
    if e.get("path") == want:
        print(e.get("size", ""))
        break
' "$want"
    fi
}

files=("$@")
sizes=()
missing=0
for f in "${files[@]}"; do
    s="$(size_for_path "$f")"
    if [[ -z "$s" ]]; then
        echo "hf-gguf-fetch.sh: $f not found in ${org_repo}'s tree listing" >&2
        missing=1
    fi
    sizes+=("$s")
done
if [[ "$missing" -eq 1 ]]; then
    exit 1
fi

overall_status=0
for i in "${!files[@]}"; do
    file="${files[$i]}"
    size="${sizes[$i]}"
    final_path="$dest_dir/$file"
    part_path="$final_path.part"

    if [[ -f "$final_path" ]]; then
        existing_size="$(stat -c %s "$final_path")"
        if [[ "$existing_size" == "$size" ]]; then
            echo "skip $size $final_path"
            continue
        fi
        echo "hf-gguf-fetch.sh: $final_path exists but size $existing_size != expected $size -- refusing to touch" >&2
        overall_status=1
        continue
    fi

    url="https://huggingface.co/${org_repo}/resolve/main/${file}"
    if ! curl -sS -L -C - --fail -o "$part_path" "$url"; then
        echo "hf-gguf-fetch.sh: download failed for $file (partial kept at $part_path)" >&2
        overall_status=1
        continue
    fi

    part_size="$(stat -c %s "$part_path")"
    if [[ "$part_size" == "$size" ]]; then
        mv -- "$part_path" "$final_path"
        echo "ok $size $final_path"
    else
        echo "MISMATCH $part_size $part_path"
        overall_status=1
    fi
done

exit "$overall_status"
