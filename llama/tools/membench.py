#!/usr/bin/env python3
# plans/qwen4exp-ram-ab-spike.md WP-1 -- a between-*states* memory microbench.
#
# This is a RATIO instrument, not an absolute STREAM figure (Assumption 3 of the plan): its
# only purpose is to compare the same two numbers -- sequential copy bandwidth and random
# 4 KiB-row gather rate -- across the three memory states (2-DIMM JEDEC, 2-DIMM EXPO, 4-DIMM)
# this repo's numpy (1.26.4) happens to run on this host. The access shape (sequential copy,
# then chunked random-row gather) approximates the PLE / MoE-expert lookup pattern the
# qwen4exp cells exercise; it is deliberately outside the bake-off harness (`eval-harness/inference_research/harness`), whose runtime is
# Python-stdlib-only, since this tool needs numpy.
#
# Safety: this benchmark must measure host memory bandwidth, never swap bandwidth. It computes
# its own peak allocation before allocating anything and refuses to run (exit 2) unless
# `MemAvailable` clears that peak plus a 2 GiB guard; it also samples `/proc/vmstat`
# `pswpin`/`pswpout` before and after and refuses the result (exit 3) if either counter grew
# during the run -- a benchmark that swapped measured the wrong thing.

import argparse
import gc
import json
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROW_WIDTH = 512  # elements per row; 512 * 8 bytes (uint64) == 4 KiB, one "row" of the PLE shape
CHUNK_ROWS = 65536  # 65536 * 4 KiB == 256 MiB reused gather buffer
GATHER_ROWS_PER_REP = 2_000_000
GATHER_SEED = 20260901
GUARD_BYTES = 2 * (1024 ** 3)  # 2 GiB


def _read_meminfo():
    values = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            rest = rest.strip()
            if rest.endswith("kB"):
                rest = rest[:-2].strip()
            try:
                values[key] = int(rest) * 1024  # normalize to bytes
            except ValueError:
                continue
    return values


def _read_vmstat_counters():
    pswpin = pswpout = None
    with open("/proc/vmstat") as f:
        for line in f:
            key, _, rest = line.partition(" ")
            if key == "pswpin":
                pswpin = int(rest.strip())
            elif key == "pswpout":
                pswpout = int(rest.strip())
    return pswpin, pswpout


def _iso_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Between-states memory bandwidth/gather-rate ratio instrument "
            "(plans/qwen4exp-ram-ab-spike.md WP-1)."
        )
    )
    parser.add_argument("--out", type=Path, default=None, help="append the JSON result line here")
    parser.add_argument("--reps", type=int, default=3, help="reps per measurement (default 3)")
    parser.add_argument("--gib", type=float, default=8, help="source array size in GiB (default 8)")
    args = parser.parse_args()

    n_elements = int(args.gib * (1024 ** 3)) // 8  # uint64 == 8 bytes/element
    n_rows = n_elements // ROW_WIDTH
    n_elements = n_rows * ROW_WIDTH  # trim to an exact multiple of the row shape
    src_bytes = n_elements * 8
    dst_bytes = src_bytes
    gather_buf_bytes = CHUNK_ROWS * ROW_WIDTH * 8
    index_bytes = GATHER_ROWS_PER_REP * 8  # np.random.default_rng().integers() default dtype
    peak_alloc_bytes = src_bytes + dst_bytes + gather_buf_bytes + index_bytes

    meminfo_before = _read_meminfo()
    mem_available = meminfo_before.get("MemAvailable")
    if mem_available is None or mem_available <= peak_alloc_bytes + GUARD_BYTES:
        print(
            f"membench: refusing to run -- MemAvailable={mem_available} bytes does not clear "
            f"peak_alloc_bytes={peak_alloc_bytes} + guard={GUARD_BYTES} bytes",
            file=sys.stderr,
        )
        sys.exit(2)

    pswpin_before, pswpout_before = _read_vmstat_counters()

    print(
        f"membench: allocating src={src_bytes / 1e9:.2f} GB dst={dst_bytes / 1e9:.2f} GB "
        f"gather_buf={gather_buf_bytes / 1e9:.3f} GB (peak ~{peak_alloc_bytes / (1024 ** 3):.2f} GiB)",
        file=sys.stderr,
    )

    src = np.arange(n_elements, dtype=np.uint64).reshape(n_rows, ROW_WIDTH)
    dst = np.empty((n_rows, ROW_WIDTH), dtype=np.uint64)

    # (a) Sequential copy bandwidth: np.copyto into the preallocated destination.
    copy_gbps = []
    for rep in range(args.reps):
        start = time.perf_counter()
        np.copyto(dst, src)
        elapsed = time.perf_counter() - start
        gbps = (src_bytes / elapsed) / 1e9
        copy_gbps.append(gbps)
        print(f"membench: copy rep {rep + 1}/{args.reps}: {gbps:.2f} GB/s", file=sys.stderr)
    copy_gbps_median = _median(copy_gbps)

    # (b) Random gather rate: fixed seed, chunked into a reused buffer -- never a full
    # materialised ~8 GB output.
    rng = np.random.default_rng(GATHER_SEED)
    gather_buf = np.empty((CHUNK_ROWS, ROW_WIDTH), dtype=np.uint64)
    gather_rows_per_s = []
    for rep in range(args.reps):
        idx = rng.integers(0, n_rows, size=GATHER_ROWS_PER_REP)
        start = time.perf_counter()
        for chunk_start in range(0, len(idx), CHUNK_ROWS):
            chunk = idx[chunk_start:chunk_start + CHUNK_ROWS]
            np.take(src, chunk, axis=0, out=gather_buf[: len(chunk)])
        elapsed = time.perf_counter() - start
        rate = GATHER_ROWS_PER_REP / elapsed
        gather_rows_per_s.append(rate)
        print(
            f"membench: gather rep {rep + 1}/{args.reps}: {rate:,.0f} rows/s", file=sys.stderr
        )
    gather_rows_per_s_median = _median(gather_rows_per_s)

    meminfo_after = _read_meminfo()
    pswpin_after, pswpout_after = _read_vmstat_counters()
    pswpin_delta = pswpin_after - pswpin_before
    pswpout_delta = pswpout_after - pswpout_before

    # Free explicitly before any --out append, so the append never races with 16+ GiB resident.
    del src, dst, gather_buf
    gc.collect()

    if pswpin_delta > 0 or pswpout_delta > 0:
        print(
            f"membench: refusing the result -- swap activity during the run "
            f"(pswpin_delta={pswpin_delta}, pswpout_delta={pswpout_delta}); "
            "this benchmark measured swap, not memory bandwidth.",
            file=sys.stderr,
        )
        sys.exit(3)

    result = {
        "timestamp": _iso_now(),
        "hostname": socket.gethostname(),
        "uname_r": platform.uname().release,
        "nproc": os.cpu_count(),
        "numpy_version": np.__version__,
        "gib": args.gib,
        "reps": args.reps,
        "peak_alloc_bytes": peak_alloc_bytes,
        "copy_gbps": copy_gbps,
        "copy_gbps_median": copy_gbps_median,
        "gather_rows_per_s": gather_rows_per_s,
        "gather_rows_per_s_median": gather_rows_per_s_median,
        "pswpin_delta": pswpin_delta,
        "pswpout_delta": pswpout_delta,
        "meminfo_before": {
            "MemTotal": meminfo_before.get("MemTotal"),
            "MemAvailable": meminfo_before.get("MemAvailable"),
        },
        "meminfo_after": {
            "MemTotal": meminfo_after.get("MemTotal"),
            "MemAvailable": meminfo_after.get("MemAvailable"),
        },
    }

    print(
        f"membench: copy median {copy_gbps_median:.2f} GB/s, "
        f"gather median {gather_rows_per_s_median:,.0f} rows/s, "
        f"pswpin_delta={pswpin_delta}, pswpout_delta={pswpout_delta}",
        file=sys.stderr,
    )

    line = json.dumps(result)
    print(line)
    if args.out is not None:
        with open(args.out, "a") as f:
            f.write(line + "\n")


if __name__ == "__main__":
    import sys

    if not {"-h", "--help"} & set(sys.argv[1:]):
        from build_id import handle_build_flags

        _build_rc = handle_build_flags(sys.argv[1:], __file__)
        if _build_rc is not None:
            sys.exit(_build_rc)
    main()
