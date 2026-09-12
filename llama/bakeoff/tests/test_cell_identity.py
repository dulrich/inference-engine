"""**The identity lock.** Every committed queue must resolve to the same ordered `cell_id` list,
byte for byte, forever.

A `cell_id` is a stored identifier in consumer systems: it names a record directory under
`llama/bakeoff/records/`, it keys every entry in a `.state.json` file, and the dashboard joins on
it. A change to it does not error -- it silently forks state, exactly like an alias rename (root
`CLAUDE.md`, "The alias contract"). So identity is frozen by an explicit golden table, not by
hoping a refactor happened to preserve it.

The golden lists below were captured at commit `6d9dd40` (pre-WP-R1) and are the contract. The
per-queue sha256 digests are a second, cheaper assertion over the same data.

This suite is entirely offline: it reads committed queue JSON and computes hashes. No Docker, no
network, no host sysfs.
"""

import hashlib
import sys
import unittest
from pathlib import Path

BAKEOFF_DIR = Path(__file__).resolve().parent.parent
if str(BAKEOFF_DIR) not in sys.path:
    sys.path.insert(0, str(BAKEOFF_DIR))

import batch  # noqa: E402
import queue_file  # noqa: E402
import sweep  # noqa: E402

QUEUES_DIR = BAKEOFF_DIR / "queues"

GOLDEN_CELL_IDS = {
    "ladder-v1": [  # 53 cells, sha256(chr(10).join(ids))[:16] == "eb3b2a5e8e089809"
        "gemma-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-e4fe5b21",
        "gemma-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-ec13812f",
        "gemma-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-3c64912f",
        "qwen-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-de58b7e6",
        "qwen-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-500122c1",
        "qwen-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-4e03948f",
        "qwen-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-f44a970b",
        "ling-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-1a3f57f4",
        "ling-c32k-f16-prod-ctkf16-ctvf16-faon-c32768-e12c9fd0",
        "ling-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-3f61ac3d",
        "ling-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-49b92124",
        "nemotron-c32k-f16-capa-ctkf16-ctvf16-faon-c32768-6ccca239",
        "nemotron-c32k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-f3b891ea",
        "nemotron-c32k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c32768-b0b368f9",
        "gemma-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-84485fc0",
        "gemma-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-bba3c2c3",
        "gemma-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-b70d3e46",
        "qwen-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-62c043a9",
        "qwen-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-6bcf1af0",
        "qwen-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-5e4b4847",
        "qwen-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-12812fed",
        "ling-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-1b5231ea",
        "ling-c64k-f16-prod-ctkf16-ctvf16-faon-c65536-63621011",
        "ling-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-0f80680c",
        "ling-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-bd7384d0",
        "nemotron-c64k-f16-capa-ctkf16-ctvf16-faon-c65536-c0c6ed03",
        "nemotron-c64k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c65536-84231024",
        "nemotron-c64k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c65536-6a755c5e",
        "gemma-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-bb2eef21",
        "gemma-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-b2a4c43c",
        "gemma-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-7f234f36",
        "qwen-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-d648829d",
        "qwen-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-439280de",
        "qwen-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-3b7aca95",
        "qwen-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-d25b7512",
        "ling-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-b6c81454",
        "ling-c128k-f16-prod-ctkf16-ctvf16-faon-c131072-f60f531f",
        "ling-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-b1de72a2",
        "ling-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-0776bda2",
        "nemotron-c128k-f16-capa-ctkf16-ctvf16-faon-c131072-627adeb0",
        "nemotron-c128k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c131072-f640c235",
        "nemotron-c128k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c131072-d80d1b92",
        "gemma-c256k-f16-capa-ctkf16-ctvf16-faon-c262144-6df1b1f7",
        "gemma-c256k-f16-prod-ctkf16-ctvf16-faon-c262144-a7a1c3d5",
        "gemma-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-e76b86cd",
        "qwen-c256k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c262144-f72351c0",
        "qwen-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-a4523784",
        "nemotron-c256k-f16-capa-ctkf16-ctvf16-faon-c262144-34ffbcc8",
        "nemotron-c256k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c262144-912bb1f4",
        "nemotron-c256k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c262144-c8eb9ef0",
        "nemotron-c512k-f16-capa-ctkf16-ctvf16-faon-c524288-26602b59",
        "nemotron-c512k-q8q8-cap-ctkq8_0-ctvq8_0-faon-c524288-ff2e2d4c",
        "nemotron-c512k-q4q4-cap-ctkq4_0-ctvq4_0-faon-c524288-993577ed",
    ],
    "matrix-v1-dedicated": [  # 4 cells, sha256(chr(10).join(ids))[:16] == "3ff4deaecf42b299"
        "mxd-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-e5a6c2af",
        "mxd-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-6ea21d2e",
        "mxd-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-eedc64d6",
        "mxd-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-535f7178",
    ],
    "matrix-v1-dedicated-prod": [  # 14 cells, sha256(chr(10).join(ids))[:16] == "bbdc3f60c9cf87fe"
        "mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
        "mxdp-qwen36-c128k-f16-timing-base-think-ctkf16-ctvf16-faauto-c131072-447e38a2",
        "mxdp-qwen36-c128k-f16-timing-mtp-nothink-ctkf16-ctvf16-faauto-c131072-27ef8a46",
        "mxdp-qwen36-c128k-f16-timing-base-nothink-ctkf16-ctvf16-faauto-c131072-60601124",
        "mxdp-ling-ud-q4-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9f2929eb",
        "mxdp-ling-ud-q6-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-85d4f581",
        "mxdp-ling-q8-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-9e8a463f",
        "mxdp-gemma-qat-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-64f3cf26",
        "mxdp-qwen36-c128k-f16-mtp-prod-ctkf16-ctvf16-faauto-c131072-f330211a",
        "mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-ctkq4_0-ctvq4_0-faon-c131072-0d8108e6",
        "mxdp-qwen36-c128k-f16-mtp-prod-nothink-ctkf16-ctvf16-faauto-c131072-2033109b",
        "mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-nothink-ctkq4_0-ctvq4_0-faon-c131072-41f57002",
        "mxdp-qwen36-c128k-f16-mtp-prod-rb128-ctkf16-ctvf16-faauto-c131072-feb547bf",
        "mxdp-qwen38-iq3s-c128k-q4q4-fa-prod-rb128-ctkq4_0-ctvq4_0-faon-c131072-62da3ac9",
    ],
    "matrix-v1-shared": [  # 18 cells, sha256(chr(10).join(ids))[:16] == "38b1f9e455113d09"
        "mx-gemma-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-1cd55be3",
        "mx-qwen36-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8d32c2d0",
        "mx-nemotron-c128k-f16-ctkf16-ctvf16-faauto-c131072-74439058",
        "mx-ling-ud-q4-c128k-f16-ctkf16-ctvf16-faauto-c131072-de5d379e",
        "mx-ling-ud-q6-c128k-f16-ctkf16-ctvf16-faauto-c131072-7a1d7924",
        "mx-ling-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-2291772d",
        "mx-qwen38-xxs-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c2573bb7",
        "mx-qwen38-iq3s-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-fee2cb77",
        "mx-qwen38-q3kxl-c64k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c65536-cd3baabe",
        "mx-gemma-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9afffa7f",
        "mx-nemotron-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-6a866129",
        "mx-ling-ud-q4-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-c18f80b4",
        "mx-nemotron-c256k-f16-ctkf16-ctvf16-faauto-c262144-4e6d7906",
        "mx-gemma-qat-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-bf0431b6",
        "mx-gemma-qat-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-df739795",
        "mx-qwen36-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-5d38307a",
        "mx-qwen36-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-41baab12",
        "mx-gemma-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-1afee66e",
    ],
    "wp5-validation": [  # 5 cells, sha256(chr(10).join(ids))[:16] == "a25b59a3c6ef2a3a"
        "gemma-qat-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-608a27f9",
        "gemma-qat-c32768-q8q8-cap-ctkq8_0-ctvq8_0-faon-c32768-74917c4b",
        "gemma-qat-c262144-f16-fitcell-ctkf16-ctvf16-faon-c262144-459ad496",
        "ling-c32768-f16-prod-ctkf16-ctvf16-faon-c32768-8c361420",
        "nemotron-c65536-f16-prodfallback-ctkf16-ctvf16-faon-c65536-26b464a7",
    ],
    "qwen4exp-viability-v1": [  # 4 cells, sha256(chr(10).join(ids))[:16] == "a5b19c63040bced4"
        "q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
        "q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
        "q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
        "q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
    ],
    # plans/qwen4exp-ram-ab-spike.md WP-1 -- three byte-identical queues (the memory state is
    # carried by the file name and measurement directory, not by content): the four
    # qwen4exp-viability-v1 cells verbatim plus the matrix-v1-dedicated-prod control cell
    # verbatim, control last. All three resolve to the same ordered five-id list.
    "qwen4exp-ram-2dimm-jedec-v1": [  # 5 cells, sha256(chr(10).join(ids))[:16] == "bb0d3aa0f7c1a683"
        "q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
        "q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
        "q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
        "q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
        "mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
    ],
    "qwen4exp-ram-2dimm-expo-v1": [  # 5 cells, sha256(chr(10).join(ids))[:16] == "bb0d3aa0f7c1a683"
        "q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
        "q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
        "q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
        "q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
        "mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
    ],
    "qwen4exp-ram-4dimm-v1": [  # 5 cells, sha256(chr(10).join(ids))[:16] == "bb0d3aa0f7c1a683"
        "q4x-cmoe-c4k-f16-ctkf16-ctvf16-faauto-c4096-d29f605c",
        "q4x-ncmoe38-c4k-f16-ctkf16-ctvf16-faauto-c4096-ea726295",
        "q4x-cmoe-c16k-f16-ctkf16-ctvf16-faauto-c16384-d181c845",
        "q4x-ncmoe38-c16k-f16-ctkf16-ctvf16-faauto-c16384-64cfbaa3",
        "mxdp-qwen36-c128k-f16-timing-mtp-think-ctkf16-ctvf16-faauto-c131072-a609f0c5",
    ],
    # plans/small-dense-decode-roofline-spike.md WP-1: 11 measurement cells (Qwen3.5-4B/0.8B
    # BF16/Q8_0/Q6_K/Q4_0, MiniCPM5-1B F16/Q8_0/Q4_K_M) plus one matched control cell against the
    # live `nemotron-4b` alias's own served file. `timing_prompt_tokens`/`timing_max_tokens` are
    # non-identity (`cell_model.QueueCellMetadata`, not `CellIdentity`) -- absent from every id.
    "small-dense-roofline-v1": [  # 12 cells, sha256(chr(10).join(ids))[:16] == "2eb1ece7aa19452e"
        "sdr-qwen35-4b-bf16-ctkf16-ctvf16-faauto-c16384-08af6261",
        "sdr-qwen35-4b-q8-ctkf16-ctvf16-faauto-c16384-6fb6a377",
        "sdr-qwen35-4b-q6k-ctkf16-ctvf16-faauto-c16384-6f308247",
        "sdr-qwen35-4b-q40-ctkf16-ctvf16-faauto-c16384-68f790d1",
        "sdr-qwen35-08b-bf16-ctkf16-ctvf16-faauto-c16384-3bf7fca9",
        "sdr-qwen35-08b-q8-ctkf16-ctvf16-faauto-c16384-45c7f57b",
        "sdr-qwen35-08b-q6k-ctkf16-ctvf16-faauto-c16384-107c38d7",
        "sdr-qwen35-08b-q40-ctkf16-ctvf16-faauto-c16384-5783a120",
        "sdr-minicpm5-1b-f16-ctkf16-ctvf16-faauto-c16384-3c124316",
        "sdr-minicpm5-1b-q8-ctkf16-ctvf16-faauto-c16384-4d2ae2db",
        "sdr-minicpm5-1b-q4km-ctkf16-ctvf16-faauto-c16384-cf888fc6",
        "sdr-nemotron-4b-udq4xl-ctl-ctkf16-ctvf16-faauto-c16384-3c7a0785",
    ],
    "qwen36-resident-v1": [  # 2 cells, sha256(chr(10).join(ids))[:16] == "c7612fcab3015526"
        "q36r-ud-iq3-xxs-ctkf16-ctvf16-faauto-c16384-7b8941de",
        "q36r-ud-iq2-m-ctkf16-ctvf16-faauto-c16384-d847b684",
    ],
    # WP-2 of plans/minicpm5-2b-context-ladder.md: headless ladder over the three vendor
    # quants, shortest-first (32k/64k/128k rungs, q4km/q8/f16 order within each rung).
    "minicpm5-2b-ladder-v1": [  # 9 cells, sha256(chr(10).join(ids))[:16] == "dd3b61c2c84eb07d"
        "mc2-q4km-c32k-f16-ctkf16-ctvf16-faauto-c32768-760418a9",
        "mc2-q8-c32k-f16-ctkf16-ctvf16-faauto-c32768-868011c9",
        "mc2-f16-c32k-f16-ctkf16-ctvf16-faauto-c32768-d8be8e51",
        "mc2-q4km-c64k-f16-ctkf16-ctvf16-faauto-c65536-0c17f00c",
        "mc2-q8-c64k-f16-ctkf16-ctvf16-faauto-c65536-846daf65",
        "mc2-f16-c64k-f16-ctkf16-ctvf16-faauto-c65536-452b1a3e",
        "mc2-q4km-c128k-f16-ctkf16-ctvf16-faauto-c131072-939ca587",
        "mc2-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-92526252",
        "mc2-f16-c128k-f16-ctkf16-ctvf16-faauto-c131072-b1b3c95c",
    ],
    # WP-2 of plans/gemma4-26b-a4b-qat-mtp-ladder.md: headless Gemma 4 26B A4B QAT + MTP
    # context ladder, shortest-first (32k/64k/128k/256k on the unsloth UD-Q4_K_XL, plus at
    # 128k a spec_decode: "base" arm and the lmstudio QAT-Q4_0 A/B cell).
    "gemma4-26b-a4b-ladder-v1": [  # 6 cells, sha256(chr(10).join(ids))[:16] == "c618905ef5cf34ed"
        "g26-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-043ba5a9",
        "g26-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-02135724",
        "g26-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-398b1f3f",
        "g26-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9ee03266",
        "g26-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-2bd65ba0",
        "g26-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-89c80044",
    ],
    # WP-2 of plans/moe-offload-load-mode-and-qwen4exp-fit-math.md: `--load-mode none` A/B,
    # dense pair first (no CPU experts, faster load), then the MoE-offload pair.
    "lm-ab-v1": [  # 4 cells, sha256(chr(10).join(ids))[:16] == "74df1f6c88682f9f"
        "lmab-qwen38-mmap-c128k-ctkq4_0-ctvq4_0-faon-c131072-f9631d23",
        "lmab-qwen38-none-c128k-ctkq4_0-ctvq4_0-faon-c131072-1f202b21",
        "lmab-qwen36-mmap-c128k-ctkf16-ctvf16-faauto-c131072-f18b0d5f",
        "lmab-qwen36-none-c128k-ctkf16-ctvf16-faauto-c131072-bb080a73",
    ],
}

GOLDEN_DIGESTS = {
    "ladder-v1": (53, "eb3b2a5e8e089809"),
    "matrix-v1-dedicated": (4, "3ff4deaecf42b299"),
    "matrix-v1-dedicated-prod": (14, "bbdc3f60c9cf87fe"),
    "matrix-v1-shared": (18, "38b1f9e455113d09"),
    "wp5-validation": (5, "a25b59a3c6ef2a3a"),
    "qwen4exp-viability-v1": (4, "a5b19c63040bced4"),
    "qwen4exp-ram-2dimm-jedec-v1": (5, "bb0d3aa0f7c1a683"),
    "qwen4exp-ram-2dimm-expo-v1": (5, "bb0d3aa0f7c1a683"),
    "qwen4exp-ram-4dimm-v1": (5, "bb0d3aa0f7c1a683"),
    "small-dense-roofline-v1": (12, "2eb1ece7aa19452e"),
    "qwen36-resident-v1": (2, "c7612fcab3015526"),
    "minicpm5-2b-ladder-v1": (9, "dd3b61c2c84eb07d"),
    "lm-ab-v1": (4, "74df1f6c88682f9f"),
    "gemma4-26b-a4b-ladder-v1": (6, "c618905ef5cf34ed"),
}


def _resolve(queue_name):
    return queue_file.resolve_queue_cells(
        queue_file.load_queue(QUEUES_DIR / f"{queue_name}.json")
    )


class TestCommittedQueueCellIdentity(unittest.TestCase):
    """Table-driven: every committed queue, its full ordered cell-ID list, and its digest."""

    def test_every_committed_queue_is_covered_by_the_golden_table(self):
        # A new committed queue must be added to the table deliberately -- never silently
        # unpinned by virtue of not being listed. `*.state.json` siblings are durable run state
        # (`queue_state.default_state_path`), not queue files, and are excluded.
        on_disk = sorted(
            p.stem for p in QUEUES_DIR.glob("*.json") if not p.name.endswith(".state.json")
        )
        self.assertEqual(sorted(GOLDEN_CELL_IDS), on_disk)

    def test_ordered_cell_ids_match_the_golden_lists(self):
        for queue_name, expected_ids in GOLDEN_CELL_IDS.items():
            with self.subTest(queue=queue_name):
                actual = [c.cell_id for c in _resolve(queue_name)]
                self.assertEqual(actual, expected_ids)

    def test_ordered_cell_id_digests_match(self):
        for queue_name, (expected_count, expected_digest) in GOLDEN_DIGESTS.items():
            with self.subTest(queue=queue_name):
                ids = [c.cell_id for c in _resolve(queue_name)]
                digest = hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]
                self.assertEqual(len(ids), expected_count)
                self.assertEqual(digest, expected_digest)

    def test_batch_re_exports_the_same_resolver(self):
        # `batch.resolve_queue_cells` is the historical public name; it must stay the same
        # function as `queue_file.resolve_queue_cells` after the WP-R1 split.
        self.assertIs(batch.resolve_queue_cells, queue_file.resolve_queue_cells)


class TestIdentityIsStructurallyIsolated(unittest.TestCase):
    """WP-R1's actual claim: non-identity facts *cannot* reach `cell_id`, rather than merely
    happening not to."""

    def test_cell_id_is_computed_solely_from_the_identity_value(self):
        for cell in _resolve("wp5-validation"):
            self.assertEqual(cell.cell_id, cell.identity.cell_id)
            self.assertEqual(
                cell.cell_id, sweep.compute_cell_id(cell.identity.identity_payload()),
            )

    def test_identity_payload_carries_exactly_four_fields(self):
        cell = _resolve("wp5-validation")[0]
        self.assertEqual(
            sorted(cell.identity.identity_payload()),
            ["alias", "model_container_path", "model_host_path", "server_flags"],
        )

    def test_identity_is_frozen_and_cannot_grow_a_metadata_field(self):
        # The pre-WP-R1 defect in one assertion: `resolve_cell` used to bolt `_trials`,
        # `_sampling_arm`, `model`, `spec_decode` and friends onto the very dict `cell_id` was
        # hashed from, with only comment discipline enforcing the ordering. Now it raises.
        import dataclasses

        cell = _resolve("wp5-validation")[0]
        for field_name in ("_trials", "trials", "model", "spec_decode", "chars_per_token"):
            with self.subTest(field=field_name):
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    setattr(cell.identity, field_name, "anything")

    def test_queue_metadata_never_reaches_the_sweep_adapter_as_identity(self):
        # The dict handed to `sweep.py` carries no queue-only metadata at all, so a future
        # `compute_cell_id` reading a stray key could not find one.
        cell = _resolve("wp5-validation")[0]
        adapter = cell.as_sweep_cell()
        for banned in ("model", "trials", "sampling_arm", "spec_decode", "timing_rounds",
                       "timing_prompt_tokens", "timing_max_tokens",
                       "chars_per_token", "_queue_model_id", "_sampling_arm", "_trials",
                       "_chars_per_token"):
            self.assertNotIn(banned, adapter)
        self.assertEqual(sweep.compute_cell_id(adapter), cell.cell_id)

    def test_varying_every_queue_metadata_field_leaves_cell_id_unchanged(self):
        import dataclasses

        cell = _resolve("wp5-validation")[0]
        baseline = cell.cell_id
        variants = [
            dataclasses.replace(cell.queue, model_id="something-else"),
            dataclasses.replace(cell.queue, sampling_arm="production"),
            dataclasses.replace(cell.queue, trials=999),
            dataclasses.replace(cell.queue, chars_per_token=4.06),
            dataclasses.replace(cell.queue, timing_rounds=11),
            # WP-1 (small-dense-decode-roofline-spike): the timing arm's optional depth/length
            # knobs -- same non-identity contract as `timing_rounds` directly above.
            dataclasses.replace(cell.queue, timing_prompt_tokens=8000),
            dataclasses.replace(cell.queue, timing_max_tokens=256),
            dataclasses.replace(cell.queue, spec_decode="mtp"),
            # WP-R2 moved `model_family` onto this value (it used to ride an untyped bag). It
            # DOES reach the sweep adapter -- deliberately, the live runner reads it there --
            # but it is not one of the four fields `compute_cell_id` hashes.
            dataclasses.replace(cell.queue, model_family="gemma4"),
        ]
        for variant in variants:
            with self.subTest(queue_meta=variant):
                mutated = dataclasses.replace(cell, queue=variant)
                self.assertEqual(mutated.cell_id, baseline)

    def test_changing_an_identity_field_does_change_cell_id(self):
        # The converse -- proving the lock is not vacuous.
        import dataclasses

        cell = _resolve("wp5-validation")[0]
        renamed = dataclasses.replace(
            cell, identity=dataclasses.replace(cell.identity, alias="different-alias"),
        )
        self.assertNotEqual(renamed.cell_id, cell.cell_id)


class RamAbQueuesAreIdentical(unittest.TestCase):
    """plans/qwen4exp-ram-ab-spike.md WP-1 -- the three RAM A/B queues are byte-identical
    files; this asserts it at the parsed-JSON level (not just cell identity) so a future edit
    that touched one queue's `models`/`cells` without touching the other two would fail here
    even if it somehow left `cell_id` unchanged."""

    QUEUE_NAMES = (
        "qwen4exp-ram-2dimm-jedec-v1",
        "qwen4exp-ram-2dimm-expo-v1",
        "qwen4exp-ram-4dimm-v1",
    )

    def _load(self, name):
        import json

        return json.loads((QUEUES_DIR / f"{name}.json").read_text())

    def test_models_and_cells_are_pairwise_equal(self):
        queues = {name: self._load(name) for name in self.QUEUE_NAMES}
        baseline_name = self.QUEUE_NAMES[0]
        baseline = queues[baseline_name]
        for name in self.QUEUE_NAMES[1:]:
            with self.subTest(queue=name):
                self.assertEqual(queues[name]["models"], baseline["models"])
                self.assertEqual(queues[name]["cells"], baseline["cells"])

    def test_cell_id_lists_equal_the_golden_jedec_entry(self):
        expected = GOLDEN_CELL_IDS["qwen4exp-ram-2dimm-jedec-v1"]
        for name in self.QUEUE_NAMES:
            with self.subTest(queue=name):
                actual = [c.cell_id for c in _resolve(name)]
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
