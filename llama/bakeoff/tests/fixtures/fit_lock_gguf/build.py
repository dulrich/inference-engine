#!/usr/bin/env python3
"""Builds the reduced real GGUF headers `tests/test_fit_lock.py` classifies against.

Each fixture is a real model's header prefix, rewritten only by dropping the `tokenizer.ggml.*`
ARRAY KVs (the vocabulary -- megabytes no fit/geometry code reads) and adjusting `kv_count`.
Magic, version, tensor_count, every other KV and the whole tensor-info array are copied
byte-for-byte. No alignment padding is written: no tensor data follows, and the genuine reader
never seeks past the tensor-info array. `manifest.json` maps each original host path to its
fixture and the real file size, which the test restores with a sparse `os.truncate`.

Sources: the local real file when present, else an HTTP Range fetch of the header prefix from
huggingface.co (never the whole file). Deterministic: same source bytes, same output bytes.

Usage: python3 build.py [--help]   (writes next to this script)
"""

import io
import json
import struct
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

import gguf_reader  # noqa: E402

HF = "https://huggingface.co/{}/resolve/main/{}"
MINICPM = "openbmb/MiniCPM5-2B-GGUF"
GEMMA_QAT = "lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF"
FLASH = "/srv/models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-{:05d}-of-00003.gguf"

# (original host path, HF repo to Range-fetch from, or None for a locally present file)
SOURCES: list[tuple[str, str | None]] = [
    ("/srv/models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q8_0.gguf", None),
    ("/srv/models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q4_K_XL.gguf", None),
    ("/srv/models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q6_K_XL.gguf", None),
    ("/srv/models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf", None),
    ("/srv/models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf", None),
    ("/srv/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf", None),
    ("/srv/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_XXS.gguf", None),
    ("/srv/models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q3_K_XL.gguf", None),
    *[(FLASH.format(i), None) for i in (1, 2, 3)],  # split siblings: the tensor-info read needs all
    ("/srv/models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf", None),
    ("/models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", None),
    ("/models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-F16.gguf", MINICPM),
    ("/models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q8_0.gguf", MINICPM),
    ("/models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q4_K_M.gguf", MINICPM),
    ("/models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/gemma-4-26B-A4B-it-QAT-Q4_0.gguf", GEMMA_QAT),
]


def _skip_string(f) -> str:
    return gguf_reader._read_gguf_string(f)


def reduce_header(prefix: bytes) -> bytes:
    """Raises `GGUFParseError` if `prefix` ends before the tensor-info array does."""
    f = io.BytesIO(prefix)
    head = gguf_reader._read_exact(f, 16)
    (kv_count,) = struct.unpack("<Q", gguf_reader._read_exact(f, 8))
    kept: list[bytes] = []
    for _ in range(kv_count):
        start = f.tell()
        key = _skip_string(f)
        (vtype,) = struct.unpack("<I", gguf_reader._read_exact(f, 4))
        gguf_reader._read_gguf_value(f, vtype)
        if not (vtype == gguf_reader._GGUF_TYPE_ARRAY and key.startswith("tokenizer.ggml.")):
            kept.append(prefix[start:f.tell()])
    tensors_start = f.tell()
    (tensor_count,) = struct.unpack("<Q", head[8:16])
    for _ in range(tensor_count):
        _skip_string(f)
        (n_dims,) = struct.unpack("<I", gguf_reader._read_exact(f, 4))
        gguf_reader._read_exact(f, 8 * n_dims + 12)
    return head + struct.pack("<Q", len(kept)) + b"".join(kept) + prefix[tensors_start:f.tell()]


def _hf_prefix(url: str, n: int) -> tuple[bytes, int]:
    req = urllib.request.Request(url, headers={"Range": f"bytes=0-{n - 1}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status != 206:
            raise SystemExit(f"{url}: expected 206 Partial Content, got {resp.status}")
        total = int(resp.headers["Content-Range"].rsplit("/", 1)[1])
        return resp.read(), total


def build_one(host_path: str, repo: str | None) -> tuple[bytes, int, int]:
    """`(reduced header, real file size, bytes fetched)` -- the prefix grows until it parses."""
    n = 8 << 20
    while True:
        if repo is None:
            with open(host_path, "rb") as fh:
                prefix, total = fh.read(n), Path(host_path).stat().st_size
        else:
            prefix, total = _hf_prefix(HF.format(repo, Path(host_path).name), n)
        try:
            return reduce_header(prefix), total, len(prefix)
        except gguf_reader.GGUFParseError:
            if len(prefix) < n:
                raise
            n *= 2


def main(argv: list[str]) -> int:
    if argv[1:2] in (["--help"], ["-h"]):
        print(__doc__)
        return 0
    manifest = {}
    for host_path, repo in SOURCES:
        data, total, fetched = build_one(host_path, repo)
        rel = Path(*Path(host_path).parts[-2:])  # <hf-repo-dir>/<file name>: keeps shard names
        (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
        (HERE / rel).write_bytes(data)
        gguf_reader._read_gguf_tensor_infos_one_shard(HERE / rel)  # genuine reader must accept it
        manifest[host_path] = {"fixture": str(rel), "size": total}
        print(f"{rel}: {len(data)} B (real {total} B, read {fetched} B from {repo or 'local'})")
    (HERE / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
