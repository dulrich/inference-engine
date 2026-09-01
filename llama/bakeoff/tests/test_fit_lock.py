"""**The fit-and-command lock.** Every committed queue must resolve to the same fit
classification and the same `llama-server` argv, byte for byte, forever.

Companion to `test_cell_identity.py` (which locks the ordered `cell_id` list). Identity says
*which* cell a record belongs to; this file says *what that cell was judged to be and what it
would actually run*. Both are consumed downstream: a fit classification gates whether a cell is
attempted at all, and the built command is the exact process a bake-off measures. A refactor
that silently shifts either does not error -- it quietly changes what the fleet runs and what
its recorded numbers mean.

Captured at commit `27e1153` (pre-WP-R3) across all 5 committed queues, 94 cells; extended at
WP-1 (`plans/qwen4exp-bakeoff-campaign.md`) to 6 committed queues, 98 cells (`qwen4exp-viability-
v1.json` joined; the five-queue/94-cell digests below are re-derived unchanged by the
WP-1 additivity proof, re-run by the orchestrator at landing -- `d8b09bba7a47d2b0`/`56bfd847eaf30d3a`, n=94):

    fit_classifications  c049cba53b8962b5  n=98
    build_commands       ec2214347c80c9e2  n=98
    vram_budgets         2f795a1ad011a72b
    geometry_shape       89c2f041b8feb1bf
    kv_bytes_table       5cc5a673e0dffe7b

The per-cell table below is the human-auditable form of the first two; the digests are a second,
cheaper assertion over the *full* fit dicts (every byte count and margin, not just the
classification string) and the full argv lists.

**Fixed VRAM injections.** `classify_fit` reads host sysfs through injectable readers, and real
readings vary per run, so every assertion here injects 12.8 GiB total / 1 GiB used. These are
never to be replaced with live reads to make a pin match -- a pin that moves with the machine is
not a pin.

`TestTheLockIsNotVacuous` proves the table can fail: perturbing a cell's `-c` changes its
classification, and perturbing its KV type changes its command. Without those, a lock that
accidentally asserted nothing would look identical to one that works.

Entirely offline: reads committed queue JSON and (for cells carrying a `model_host_path`) GGUF
headers already on disk. No Docker, no network, no live sysfs.
"""

import contextlib
import functools
import hashlib
import json
import shlex
import sys
import unittest
from pathlib import Path
from typing import List, NamedTuple
from unittest import mock

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import gguf_fit  # noqa: E402
import sweep  # noqa: E402

QUEUES_DIR = BAKEOFF_DIR / "queues"

# The pinned injections (12.8 GiB total / 1 GiB used) -- see this module's docstring.
PINNED_VRAM_TOTAL_BYTES = 13744734208
PINNED_VRAM_USED_BYTES = 1073741824
PINNED_COMMAND_PORT = 8080


def _vram_total() -> int:
    return PINNED_VRAM_TOTAL_BYTES


def _vram_used() -> int:
    return PINNED_VRAM_USED_BYTES


class GoldenCell(NamedTuple):
    cell_id: str
    classification: str
    command: str


class GoldenQueue(NamedTuple):
    vram_profile: str
    cells: List[GoldenCell]


GOLDEN = {
    "ladder-v1": GoldenQueue(
        vram_profile="desktop-shared",
        cells=[
            GoldenCell(
                cell_id="gemma-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-e4fe5b21",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-ec13812f",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-3c64912f",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="qwen-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-de58b7e6",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-500122c1",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-4e03948f",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-f44a970b",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="ling-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-1a3f57f4",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-e12c9fd0",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-3f61ac3d",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 32768 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-49b92124",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 32768 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-6ccca239",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-f3b891ea",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 32768 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-b0b368f9",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 32768 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-84485fc0",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-bba3c2c3",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-b70d3e46",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="qwen-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-62c043a9",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-6bcf1af0",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-5e4b4847",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-12812fed",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="ling-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-1b5231ea",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-63621011",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-0f80680c",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 65536 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-bd7384d0",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 65536 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-c0c6ed03",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-84231024",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 65536 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-6a755c5e",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 65536 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-bb2eef21",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-b2a4c43c",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-7f234f36",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="qwen-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-d648829d",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-439280de",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-3b7aca95",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-d25b7512",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="ling-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-b6c81454",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-f60f531f",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-b1de72a2",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 131072 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-0776bda2",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-627adeb0",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-f640c235",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 131072 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-d80d1b92",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c256k-f16-capa-ctkf16-ctvf16-faon-c262144-6df1b1f7",
                classification="excluded",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c256k-f16-prod-ctkf16-ctvf16-faon-c262144-a7a1c3d5",
                classification="excluded",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-e76b86cd",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="qwen-c256k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c262144-f72351c0",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="qwen-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-a4523784",
                classification="unknown",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32",
            ),
            GoldenCell(
                cell_id="nemotron-c256k-f16-capa-ctkf16-ctvf16-faon-c262144-34ffbcc8",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c256k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c262144-912bb1f4",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 262144 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-c8eb9ef0",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 262144 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c512k-f16-capa-ctkf16-ctvf16-faon-c524288-26602b59",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 524288 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c512k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c524288-ff2e2d4c",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 524288 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c512k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c524288-993577ed",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 524288 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
        ],
    ),
    "matrix-v1-dedicated": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="mxd-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-e5a6c2af",
                classification="marginal",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mxd-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-6ea21d2e",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxd-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-eedc64d6",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mxd-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-535f7178",
                classification="fits",
                command="llama-server -m /models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
        ],
    ),
    "matrix-v1-dedicated-prod": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-base-think-ctkf16-ctvf16-faauto-c131072-447e38a2",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-mtp-nothink-ctkf16-ctvf16-faauto-c131072-27ef8a46",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --reasoning off --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-base-nothink-ctkf16-ctvf16-faauto-c131072-60601124",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --reasoning off -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-ling-ud-q4-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9f2929eb",
                classification="fits",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mxdp-ling-ud-q6-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-85d4f581",
                classification="fits",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q6_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mxdp-ling-q8-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9e8a463f",
                classification="marginal",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q8_0.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mxdp-gemma-qat-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-64f3cf26",
                classification="marginal",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-f330211a",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-ctkq4_0-ctvq4_0-faon-c131072-0d8108e6",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-mtp-prod-nothink-ctkf16-ctvf16-faauto-c131072-2033109b",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --reasoning off --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-nothink-ctkq4_0-ctvq4_0-faon-c131072-41f57002",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja --reasoning off -lv 9",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-mtp-prod-rb128-ctkf16-ctvf16-faauto-c131072-feb547bf",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --reasoning-budget 128 --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-rb128-ctkq4_0-ctvq4_0-faon-c131072-62da3ac9",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja --reasoning-budget 128 -lv 9",
            ),
        ],
    ),
    "matrix-v1-shared": GoldenQueue(
        vram_profile="desktop-shared",
        cells=[
            GoldenCell(
                cell_id="mx-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-1cd55be3",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mx-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8d32c2d0",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mx-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-74439058",
                classification="fits",
                command="llama-server -m /models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-ling-ud-q4-c128k-f16-ctkf16-ctvf16-faauto-c131072-de5d379e",
                classification="fits",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-ling-ud-q6-c128k-f16-ctkf16-ctvf16-faauto-c131072-7a1d7924",
                classification="fits",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q6_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-ling-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-2291772d",
                classification="fits",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q8_0.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-qwen38-xxs-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c2573bb7",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_XXS.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-fee2cb77",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-qwen38-q3kxl-c64k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c65536-cd3baabe",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q3_K_XL.gguf --port 8080 -c 65536 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9afffa7f",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf -lv 9",
            ),
            GoldenCell(
                cell_id="mx-qwen36-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-41baab12",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-bf0431b6",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mx-qwen36-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-5d38307a",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mx-nemotron-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-6a866129",
                classification="fits",
                command="llama-server -m /models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-ling-ud-q4-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c18f80b4",
                classification="fits",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-df739795",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-1afee66e",
                classification="excluded",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mx-nemotron-c256k-f16-ctkf16-ctvf16-faauto-c262144-4e6d7906",
                classification="fits",
                command="llama-server -m /models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
        ],
    ),
    "wp5-validation": GoldenQueue(
        vram_profile="desktop-shared",
        cells=[
            GoldenCell(
                cell_id="gemma-qat-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-608a27f9",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-qat-c32768-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-74917c4b",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk q8_0 -ctv q8_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="gemma-qat-c262144-f16-fitcell-ctkf16-ctvf16-faon-c262144-459ad496",
                classification="excluded",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="ling-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-8c361420",
                classification="unknown",
                command="llama-server -m /models/bloomer010/Ling-3.0-tiny-GGUF/Ling-3.0-tiny-Q4_K_M.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="nemotron-c65536-f16-prodfallback-ctkf16-ctvf16-faon-c65536-26b464a7",
                classification="unknown",
                command="llama-server -m /models/lmstudio-community/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-Q4_K_M.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
        ],
    ),
    # WP-1 (qwen4exp-bakeoff-campaign.md): both `-cmoe` cells classify `fits`, both `-ncmoe 38`
    # cells classify `excluded` -- pinned by the plan (cycle-3 B1) and hand-verified against the
    # 33,792 B/token (24,576 main + 9,216 indexer) KV rate before being pinned here (see this
    # WP's report for the margin arithmetic). An `excluded` `-ncmoe 38` cell here is the
    # classifier's conservative verdict under the lock's injected 11.3 GiB headless budget, not
    # a broken input -- the queue runner never consults this classification (batch.py only calls
    # classify_fit from _dry_run_report); all four cells still execute in a live run.
    "qwen4exp-viability-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
        ],
    ),
    # plans/qwen4exp-ram-ab-spike.md WP-1 -- three byte-identical queues (four qwen4exp-
    # viability-v1 cells plus the matrix-v1-dedicated-prod control cell, control last); same
    # `-cmoe`/`-ncmoe 38` fits/excluded flicker as qwen4exp-viability-v1 above (same cells,
    # same headless injection), plus the control cell's `fits`/command copied from its existing
    # entry under matrix-v1-dedicated-prod (same cell_id, same headless injection).
    "qwen4exp-ram-2dimm-jedec-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
        ],
    ),
    "qwen4exp-ram-2dimm-expo-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
        ],
    ),
    "qwen4exp-ram-4dimm-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 4096 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -cmoe -lm mmap -lv 9 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-Flash-Next-GGUF/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf --port 8080 -c 16384 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lm mmap -lv 9 -ncmoe 38 -ot 'per_layer_token_embd\\.weight=CPU'",
            ),
            GoldenCell(
                cell_id="mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
        ],
    ),
}

# sha256(json.dumps(x, sort_keys=True, default=str))[:16] over the FULL per-cell fit dicts and
# the FULL argv lists, in committed-queue order across all 9 queues (WP-1 of
# plans/qwen4exp-ram-ab-spike.md added the three qwen4exp-ram-* queues; the pre-WP-1 6-queue/
# 98-cell digests were re-derived unchanged by this WP's additivity proof -- see this file's
# module docstring).
GOLDEN_FIT_DIGEST = "6671bd6204d1e66d"
GOLDEN_COMMAND_DIGEST = "97ddbd1bdfe55d6a"
GOLDEN_CELL_COUNT = 113

# `compute_vram_budget_bytes` for both profiles under the pinned injections. `desktop-shared` is
# the static REALISTIC_FREE_VRAM_GIB cap and touches neither reader; `headless` is
# total - used - HEADLESS_PINNED_MARGIN_GIB.
GOLDEN_VRAM_BUDGETS = {
    "desktop-shared": 13743895347.2,
    "headless": 12134121472.0,
}

# A minimal scalar-`head_count_kv` llama header: 32 blocks, 8 KV heads, K/V length 128.
GEOMETRY_PROBE_METADATA = {
    "general.architecture": "llama",
    "llama.block_count": 32,
    "llama.attention.head_count_kv": 8,
    "llama.attention.key_length": 128,
    "llama.attention.value_length": 128,
}
GOLDEN_GEOMETRY = {
    "architecture": "llama",
    "block_count": 32,
    "head_count": None,
    "head_count_kv": 8,
    "key_length": 128,
    "value_length": 128,
    "context_length": None,
    "sliding_window": None,
    "sliding_window_pattern": None,
}

# bytes/token for that geometry across the KV-type ladder this repo actually runs. Hand-checkable:
# 32 layers * 8 heads * 128 elems * (bpe(ctk) + bpe(ctv)), with bpe(f16)=2, bpe(q8_0)=34/32,
# bpe(q4_0)=18/32.
GOLDEN_KV_BYTES_PER_TOKEN = {
    ("f16", "f16"): 131072.0,
    ("f16", "q8_0"): 100352.0,
    ("q8_0", "f16"): 100352.0,
    ("q8_0", "q8_0"): 69632.0,
    ("q4_0", "f16"): 83968.0,
    ("q4_0", "q8_0"): 53248.0,
}


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


@contextlib.contextmanager
def _memoized_gguf_reader():
    """Reads each distinct GGUF header ONCE per run instead of once per cell.

    `read_gguf_metadata` parses a model's entire metadata block, tokenizer vocabulary included --
    ~0.14 s per call against these headers. The 94 cells reference only a handful of distinct
    model files, so a naive table walk spends ~13 s re-parsing the same files over and over,
    against a whole-suite baseline of ~2.5 s.

    This wraps the REAL reader in an `lru_cache` for the duration of the computation: same
    parser, same bytes off disk, same results (a model file cannot change mid-run) -- it is a
    cache, never a fake. Nothing about what is being locked changes.
    """
    cached = functools.lru_cache(maxsize=None)(gguf_fit.read_gguf_metadata)
    with mock.patch.object(gguf_fit, "read_gguf_metadata", cached):
        yield


def _resolved_sweep_cells(queue_name):
    """`(vram_profile, [plain-dict cell, ...])` for one committed queue, through the exact same
    path `batch.py` uses live: `load_queue` -> `resolve_queue_cells` -> `as_sweep_cell()`."""
    queue = batch.load_queue(QUEUES_DIR / f"{queue_name}.json")
    return queue["vram_profile"], [c.as_sweep_cell() for c in batch.resolve_queue_cells(queue)]


def _compute(queue_name):
    vram_profile, cells = _resolved_sweep_cells(queue_name)
    return {
        "vram_profile": vram_profile,
        "cells": cells,
        "fits": [sweep.classify_fit(c, vram_profile, _vram_total, _vram_used) for c in cells],
        "commands": [sweep.build_command(c, PINNED_COMMAND_PORT) for c in cells],
    }


_RESULTS = None


def _results():
    """Every queue's cells, fit classifications, and built commands -- computed once for the
    whole module, in committed-queue order."""
    global _RESULTS
    if _RESULTS is None:
        with _memoized_gguf_reader():
            _RESULTS = {name: _compute(name) for name in GOLDEN}
    return _RESULTS


class TestCommittedQueueFitClassifications(unittest.TestCase):
    """Every committed cell classifies exactly as it did at `27e1153`."""

    def test_every_cell_classifies_identically(self):
        for queue_name, golden in GOLDEN.items():
            computed = _results()[queue_name]
            self.assertEqual(computed["vram_profile"], golden.vram_profile, queue_name)
            self.assertEqual(len(computed["cells"]), len(golden.cells), f"{queue_name}: count")
            for cell, fit, expected in zip(computed["cells"], computed["fits"], golden.cells):
                with self.subTest(queue=queue_name, cell_id=expected.cell_id):
                    self.assertEqual(cell["cell_id"], expected.cell_id)
                    self.assertEqual(fit["classification"], expected.classification)

    def test_full_fit_dicts_match_the_pinned_digest(self):
        """Locks every number behind the classification -- weights, KV bytes, budget, margin --
        not merely the four-way classification string."""
        fits = [f for name in GOLDEN for f in _results()[name]["fits"]]
        self.assertEqual(len(fits), GOLDEN_CELL_COUNT)
        self.assertEqual(_digest(fits), GOLDEN_FIT_DIGEST)


class TestCommittedQueueBuiltCommands(unittest.TestCase):
    """Every committed cell renders the same `llama-server` argv it did at `27e1153`."""

    def test_every_cell_builds_the_same_command(self):
        for queue_name, golden in GOLDEN.items():
            computed = _results()[queue_name]
            for argv, expected in zip(computed["commands"], golden.cells):
                with self.subTest(queue=queue_name, cell_id=expected.cell_id):
                    self.assertEqual(" ".join(shlex.quote(a) for a in argv), expected.command)

    def test_full_argv_lists_match_the_pinned_digest(self):
        cmds = [c for name in GOLDEN for c in _results()[name]["commands"]]
        self.assertEqual(len(cmds), GOLDEN_CELL_COUNT)
        self.assertEqual(_digest(cmds), GOLDEN_COMMAND_DIGEST)


class TestVRAMBudgetsAndKVArithmetic(unittest.TestCase):
    """The pure inputs every classification is built out of."""

    def test_vram_budgets_for_both_profiles(self):
        budgets = {
            p: sweep.compute_vram_budget_bytes(p, _vram_total, _vram_used)
            for p in sweep.VRAM_PROFILES
        }
        self.assertEqual(budgets, GOLDEN_VRAM_BUDGETS)

    def test_attention_geometry_shape(self):
        self.assertEqual(sweep.gguf_attention_geometry(GEOMETRY_PROBE_METADATA), GOLDEN_GEOMETRY)

    def test_kv_bytes_per_token_table(self):
        geometry = sweep.gguf_attention_geometry(GEOMETRY_PROBE_METADATA)
        for (ctk, ctv), expected in GOLDEN_KV_BYTES_PER_TOKEN.items():
            with self.subTest(ctk=ctk, ctv=ctv):
                self.assertEqual(sweep.kv_bytes_per_token(geometry, ctk, ctv), expected)


class TestTheLockIsNotVacuous(unittest.TestCase):
    """Negative controls. A golden table that cannot fail is not a lock, and these three prove
    the assertions above are actually sensitive to the things they claim to pin."""

    def _perturbed(self, flag, value):
        computed = _results()["ladder-v1"]
        cell = dict(computed["cells"][0])
        cell["server_flags"] = dict(cell["server_flags"])
        cell["server_flags"][flag] = value
        return computed["vram_profile"], cell

    def test_a_larger_context_ceiling_changes_the_classification(self):
        computed = _results()["ladder-v1"]
        baseline = computed["fits"][0]
        self.assertEqual(baseline["classification"], GOLDEN["ladder-v1"].cells[0].classification)

        vram_profile, cell = self._perturbed(
            "-c", computed["cells"][0]["server_flags"]["-c"] * 8
        )
        with _memoized_gguf_reader():
            moved = sweep.classify_fit(cell, vram_profile, _vram_total, _vram_used)
        self.assertNotEqual(moved["classification"], baseline["classification"])
        self.assertEqual(moved["classification"], "excluded")

    def test_a_different_kv_type_changes_the_built_command(self):
        baseline = _results()["ladder-v1"]["commands"][0]
        self.assertEqual(
            " ".join(shlex.quote(a) for a in baseline), GOLDEN["ladder-v1"].cells[0].command
        )
        _, cell = self._perturbed("-ctk", "q4_0")
        self.assertNotEqual(sweep.build_command(cell, PINNED_COMMAND_PORT), baseline)

    def test_perturbing_one_cell_breaks_the_cross_queue_digests(self):
        computed = _results()["ladder-v1"]
        vram_profile, cell = self._perturbed(
            "-c", computed["cells"][0]["server_flags"]["-c"] * 8
        )
        with _memoized_gguf_reader():
            fits = [sweep.classify_fit(cell, vram_profile, _vram_total, _vram_used)]
            fits += computed["fits"][1:]
        cmds = [sweep.build_command(cell, PINNED_COMMAND_PORT), *computed["commands"][1:]]
        self.assertNotEqual(_digest(fits), GOLDEN_FIT_DIGEST)
        self.assertNotEqual(_digest(cmds), GOLDEN_COMMAND_DIGEST)


if __name__ == "__main__":
    unittest.main()
