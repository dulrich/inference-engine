"""Golden campaign data for `test_fit_lock.py` (WP-R6b of
`plans/post-audit-architecture-and-reconciliation-remediation.md`; review finding F6 -- file
size). Extracted verbatim from `test_fit_lock.py`, which had grown to 1,126 lines by carrying
this data table alongside its test logic.

Data only: two `NamedTuple` golden-value containers, the per-committed-queue campaign table
(`GOLDEN`, 13 queues / 138 cells) and its pinned digests, and the small synthetic-geometry
tables the lock's "pure inputs" test pins them against. No test logic, no assertions, no
imports of `sweep`/`gguf_fit`/`batch` -- `test_fit_lock.py` is this module's only consumer.
Every literal value here is unchanged from the pre-split file; see `test_fit_lock.py`'s own
module docstring for the campaign history (which queue/WP extended the table when) and for why
these numbers are independently hand-checkable rather than derived from the implementation.
"""

from typing import List, NamedTuple


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
                cell_id="mx-nemotron-c256k-f16-ctkf16-ctvf16-faauto-c262144-4e6d7906",
                classification="fits",
                command="llama-server -m /models/unsloth/NVIDIA-Nemotron-3-Nano-4B-GGUF/NVIDIA-Nemotron-3-Nano-4B-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-bf0431b6",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c128k-q4q4-fa-ctkq4_0-ctvq4_0-faon-c131072-df739795",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
            ),
            GoldenCell(
                cell_id="mx-qwen36-c128k-f16-prod-ctkf16-ctvf16-faauto-c131072-5d38307a",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mx-qwen36-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-41baab12",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="mx-gemma-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-1afee66e",
                classification="excluded",
                command="llama-server -m /models/unsloth/gemma-4-12B-it-qat-GGUF/gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --mmproj /models/unsloth/gemma-4-12B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-12B-it-qat-GGUF/mtp-gemma-4-12B-it.gguf",
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
    # WP-2 of plans/minicpm5-2b-context-ladder.md: headless ladder over the three vendor
    # quants (real GGUF headers on disk from WP-1). Under this file's pinned 12.8 GiB
    # total / 1 GiB used injections, the F16 c128k cell's predicted margin (~0.86 GiB)
    # falls below MARGINAL_MARGIN_GIB and locks "excluded" -- a property of the fixed test
    # injection, not of the real card (the WP-3 preflight dry-run uses the live/desktop-shared
    # budget and classified all 9 cells "fits").
    "minicpm5-2b-ladder-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="mc2-q4km-c32k-f16-ctkf16-ctvf16-faauto-c32768-760418a9",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q4_K_M.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-q8-c32k-f16-ctkf16-ctvf16-faauto-c32768-868011c9",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q8_0.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-f16-c32k-f16-ctkf16-ctvf16-faauto-c32768-d8be8e51",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-F16.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-q4km-c64k-f16-ctkf16-ctvf16-faauto-c65536-0c17f00c",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q4_K_M.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-q8-c64k-f16-ctkf16-ctvf16-faauto-c65536-846daf65",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q8_0.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-f16-c64k-f16-ctkf16-ctvf16-faauto-c65536-452b1a3e",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-F16.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-q4km-c128k-f16-ctkf16-ctvf16-faauto-c131072-939ca587",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q4_K_M.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-q8-c128k-f16-ctkf16-ctvf16-faauto-c131072-92526252",
                classification="fits",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-Q8_0.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="mc2-f16-c128k-f16-ctkf16-ctvf16-faauto-c131072-b1b3c95c",
                classification="excluded",
                command="llama-server -m /models/openbmb/MiniCPM5-2B-GGUF/MiniCPM5-2B-F16.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
        ],
    ),
    # WP-2 of plans/moe-offload-load-mode-and-qwen4exp-fit-math.md: `--load-mode none` A/B --
    # dense qwen38-iq3s control pair first (no CPU experts, faster load), then the MoE-offload
    # qwen36-a3b-mtp pair (`-ncmoe 32`). Both mmap/none pairs classify and price IDENTICALLY
    # (weights/KV/margin bytes unchanged) -- `-lm` is not a placement flag, Decisions locked 8 --
    # the only argv difference is `-lm none` itself. The dense pair locks "excluded" under this
    # file's pinned 12.8 GiB total / 1 GiB used injection: same model, same flags, same c/KV as
    # the already-committed `mxd-qwen38-iq3s-c128k-q4q4-fa` cell under `matrix-v1-dedicated`
    # above (also "excluded" here), because `qwen38-iq3s` carries no `gpu_weights_bytes`
    # override and the whole 12.04 GiB file plus 9.14 GiB of KV at 131072/q4_0/q4_0 exceeds even
    # the live nameplate -- a property of the fixed test injection and the file-size fallback,
    # not evidence the recipe is broken (WP-1's live measurement runs this exact recipe).
    "lm-ab-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="lmab-qwen38-mmap-c128k-ctkq4_0-ctvq4_0-faon-c131072-f9631d23",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lv 9",
            ),
            GoldenCell(
                cell_id="lmab-qwen38-none-c128k-ctkq4_0-ctvq4_0-faon-c131072-1f202b21",
                classification="excluded",
                command="llama-server -m /models/unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-IQ3_S.gguf --port 8080 -c 131072 -ctk q4_0 -ctv q4_0 -fa on -ngl 99 --host 0.0.0.0 --jinja -lm none -lv 9",
            ),
            GoldenCell(
                cell_id="lmab-qwen36-mmap-c128k-ctkf16-ctvf16-faauto-c131072-f18b0d5f",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lv 9 -ncmoe 32 -ub 2048",
            ),
            GoldenCell(
                cell_id="lmab-qwen36-none-c128k-ctkf16-ctvf16-faauto-c131072-bb080a73",
                classification="fits",
                command="llama-server -m /models/unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --spec-type draft-mtp -lm none -lv 9 -ncmoe 32 -ub 2048",
            ),
        ],
    ),
    # WP-2 of plans/gemma4-26b-a4b-qat-mtp-ladder.md: the Gemma 4 26B A4B QAT + MTP context
    # ladder, shortest-first. First committed queue whose model carries the FUSED
    # `ffn_gate_up_exps` tensor, so it is also the first whose placement depends on this WP's
    # `_ROUTED_EXPERT_RE` fix -- the 126-cell digests above were re-derived UNCHANGED with the
    # fixed regex before this append (no pre-existing committed queue's model has that tensor),
    # which is the additivity proof for this extension.
    #
    # `-ncmoe` per rung (14/14/18/24) is the plan's floor, derived from the LIVE budget the
    # dry-run uses (`desktop-shared`, 12.8 GiB) MINUS the ~1.36 GiB of `--mmproj` clip + `-md`
    # drafter allocation that `gguf_fit` does not model at all. This file's pinned headless
    # budget is 11.30 GiB -- smaller -- so `g26-qat-c64k-f16-mtp` locks "marginal" here
    # (predicted margin 1.87 GiB, just under MARGINAL_MARGIN_GIB) while the same cell is "fits"
    # under the dry-run's 12.8 GiB. That is a property of this file's fixed injection, not of
    # the real card, and `-ncmoe` is never tuned to make it go away.
    "gemma4-26b-a4b-ladder-v1": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="g26-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-043ba5a9",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 14 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-02135724",
                classification="marginal",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 14 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-398b1f3f",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 18 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-9ee03266",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf -lv 9 -ncmoe 18 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-2bd65ba0",
                classification="fits",
                command="llama-server -m /models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/gemma-4-26B-A4B-it-QAT-Q4_0.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/mmproj-gemma-4-26B-A4B-it-QAT-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 18 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-89c80044",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 24 -ub 2048",
            ),
        ],
    ),
    # WP-2b of plans/gemma4-26b-a4b-qat-mtp-ladder.md: the same ladder run with thinking OFF
    # (`"--reasoning": "off"` in every registry entry, so it lands in each argv between
    # `--mmproj` and `--spec-type` and shapes every cell id). v1 measures Gemma 4 in its
    # default thinking mode; v2 is the needle-recall measurement. A queue is immutable once its
    # `.state.json` exists, so this is a NEW queue, never an edit of v1 -- the 132-cell digests
    # above were re-derived unchanged before this append (additivity proof).
    #
    # `-ncmoe` per rung (14/14/18/24; the q40 entry uses the 128k value) is v1's finals
    # verbatim, and the 11.30 GiB headless injection here makes `g26nt-qat-c64k-f16-mtp` lock
    # "marginal" for exactly the reason its v1 twin does (predicted margin 1.87 GiB, just under
    # MARGINAL_MARGIN_GIB, against "fits" under the dry-run's 12.8 GiB `desktop-shared`). That
    # is a property of this file's fixed injection, not of the real card; `-ncmoe` is never
    # tuned to it.
    "gemma4-26b-a4b-ladder-v2": GoldenQueue(
        vram_profile="headless",
        cells=[
            GoldenCell(
                cell_id="g26nt-qat-c32k-f16-mtp-ctkf16-ctvf16-faauto-c32768-887b1d4e",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 32768 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --reasoning off --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 14 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26nt-qat-c64k-f16-mtp-ctkf16-ctvf16-faauto-c65536-e2862bd9",
                classification="marginal",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 65536 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --reasoning off --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 14 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26nt-qat-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-8b16b9dc",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --reasoning off --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 18 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26nt-qat-c128k-f16-base-ctkf16-ctvf16-faauto-c131072-8f9356bd",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --reasoning off -lv 9 -ncmoe 18 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26nt-q40-c128k-f16-mtp-ctkf16-ctvf16-faauto-c131072-f55449e1",
                classification="fits",
                command="llama-server -m /models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/gemma-4-26B-A4B-it-QAT-Q4_0.gguf --port 8080 -c 131072 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/lmstudio-community/gemma-4-26B-A4B-it-QAT-GGUF/mmproj-gemma-4-26B-A4B-it-QAT-BF16.gguf --reasoning off --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 18 -ub 2048",
            ),
            GoldenCell(
                cell_id="g26nt-qat-c256k-f16-mtp-ctkf16-ctvf16-faauto-c262144-c14c8df3",
                classification="fits",
                command="llama-server -m /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf --port 8080 -c 262144 -ctk f16 -ctv f16 -fa auto -ngl 99 --host 0.0.0.0 --jinja --load-mode none --mmproj /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mmproj-BF16.gguf --reasoning off --spec-type draft-mtp -lv 9 -md /models/unsloth/gemma-4-26B-A4B-it-qat-GGUF/mtp-gemma-4-26B-A4B-it.gguf -ncmoe 24 -ub 2048",
            ),
        ],
    ),
}

# sha256(json.dumps(x, sort_keys=True, default=str))[:16] over the FULL per-cell fit dicts and
# the FULL argv lists, in committed-queue order across all 13 queues (WP-2b of
# plans/gemma4-26b-a4b-qat-mtp-ladder.md added gemma4-26b-a4b-ladder-v2, the no-think twin of
# the v1 ladder; the pre-WP-2b 12-queue/132-cell digests (51845f5f73509e9d/0a984493131e9630)
# were re-derived unchanged before that append, which is this WP's additivity proof -- see this
# file's module docstring).
GOLDEN_FIT_DIGEST = "e9c8494cf5ab38ca"
GOLDEN_COMMAND_DIGEST = "2571ce75b9bd783c"
GOLDEN_CELL_COUNT = 138

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

