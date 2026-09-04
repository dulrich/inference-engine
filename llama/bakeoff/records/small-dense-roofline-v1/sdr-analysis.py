#!/usr/bin/env python3
"""WP-3 analysis for plans/small-dense-decode-roofline-spike.md: per-cell measured decode vs the
model_scoreboard dense roofline (384 / weightsGB, decimal), implied utilization, admissibility,
per-family least-squares fits, and the candidate-estimator scoring table. Reads the records
(never modifies them); writes nothing except stdout."""
import json, statistics, sys
from pathlib import Path

RUN = Path(sys.argv[1] if len(sys.argv) > 1 else "llama/bakeoff/records/small-dense-roofline-v1")
CTL = Path("llama/bakeoff/records/small-dense-roofline-v1-router-control/control.json")
BW = 640e9          # RX 9070 nameplate bytes/s (scoreboard "Local workstation" profile)
UTIL = 0.6          # DECODE_BANDWIDTH_UTILIZATION
MIN_DEPTH = 4000    # WP-3 deviation: the scoreboard's 8k bucket floor in practice (capture.ts:28,47), not the plan's 7000
MIN_COMPL = 200

def admissible(s):
    return s.get("success") and s.get("finish_reason") == "length" and (s.get("completion_tokens") or 0) >= MIN_COMPL and (s.get("prompt_tokens") or 0) >= MIN_DEPTH

def analyze_rounds(samples):
    hot = [s for s in samples if s.get("run_state") == "hot" and not s.get("note")]
    cold = [s for s in samples if s.get("run_state") == "cold"]
    adm = [s for s in hot if admissible(s)]
    return {
        "rounds": len(hot) + len(cold), "hot_admissible": len(adm),
        "decode_med": statistics.median(s["observed_decode_tok_s"] for s in adm) if adm else None,
        "server_eval_med": statistics.median(s["server_eval_tok_s"] for s in adm) if adm else None,
        "cold_decode": cold[0]["observed_decode_tok_s"] if cold else None,
        "cold_prefill": cold[0]["server_prompt_eval_tok_s"] if cold else None,
        "prompt_tokens": sorted({s.get("prompt_tokens") for s in hot + cold}),
        "completion": sorted({(s.get("completion_tokens"), s.get("finish_reason")) for s in hot + cold}),
        "inadmissible": [(s["run_state"], s["round_index"], s.get("finish_reason"), s.get("completion_tokens"), s.get("prompt_tokens")) for s in hot if not admissible(s)],
    }

rows = []
for d in sorted(RUN.iterdir()):
    cfg = d / "config.json"
    if not cfg.exists():
        continue
    c = json.load(open(cfg))
    samples = [json.loads(l) for l in open(d / "samples.jsonl")] if (d / "samples.jsonl").exists() else []
    a = analyze_rounds([s for s in samples if s.get("type") == "timing_round"])
    wb = c["lineage"]["file_size"]
    gb = wb / 1e9
    est = UTIL * BW / wb
    meas = a["decode_med"]
    rows.append(dict(alias=c["sweep_cell"]["alias"], file=c["lineage"]["filename"], bytes=wb, gb=gb, est=est, meas=meas,
                     ratio=(meas / est) if meas else None, util=(meas * wb / BW) if meas else None,
                     ms_per_tok=(1000 / meas) if meas else None, vram_loaded=c.get("vram", {}).get("loaded_bytes"),
                     geometry=c.get("timing_geometry"), **a))

ctl = json.load(open(CTL)) if CTL.exists() else None
ctl_a = analyze_rounds(ctl["samples"]) if ctl else None

print("| cell | file | bytes | GB | est 384/GB | measured hot-median tok/s | ratio | util | ms/tok | server_eval | cold decode | cold prefill tok/s | prompt_tokens | (compl, finish) | adm/hot |")
print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|")
for r in rows:
    f = lambda v, p=1: "—" if v is None else f"{v:.{p}f}"
    print(f"| `{r['alias']}` | {r['file']} | {r['bytes']:,} | {r['gb']:.3f} | {r['est']:.1f} | **{f(r['meas'])}** | {f(r['ratio'],2)} | {f(r['util'],3)} | {f(r['ms_per_tok'],2)} | {f(r['server_eval_med'])} | {f(r['cold_decode'])} | {f(r['cold_prefill'],0)} | {r['prompt_tokens']} | {r['completion']} | {r['hot_admissible']}/{r['rounds']-1} |")
    if r["inadmissible"]:
        print(f"|   ↳ inadmissible hot rounds: {r['inadmissible']} |")
if ctl_a:
    print("\nControl (router path, alias nemotron-4b):", json.dumps({k: ctl_a[k] for k in ("decode_med", "server_eval_med", "cold_decode", "cold_prefill", "prompt_tokens", "completion", "hot_admissible")}))
    sc = next((r for r in rows if r["alias"].endswith("-ctl")), None)
    if sc and sc["meas"]:
        print(f"router/scratch ratio = {ctl_a['decode_med']:.2f} / {sc['meas']:.2f} = {ctl_a['decode_med']/sc['meas']:.4f}")

# --- per-family least-squares fits: ms/token = a * GB + floor -------------------------------
LAYERS = {"qwen35-4b": 32, "qwen35-08b": 24, "minicpm5": 24, "nemotron": None}
def layers(alias):
    return next((v for k, v in LAYERS.items() if k in alias), None)
def ols(pts):
    n = len(pts); sx = sum(x for x, _ in pts); sy = sum(y for _, y in pts)
    sxx = sum(x * x for x, _ in pts); sxy = sum(x * y for x, y in pts)
    a = (n * sxy - sx * sy) / (n * sxx - sx * sx); return a, (sy - a * sx) / n
print("\n| family | layers | slope ms/GB | effective GB/s | floor ms/token | floor per layer |\n|---|---:|---:|---:|---:|---:|")
for fam, L in LAYERS.items():
    pts = [(r["gb"], r["ms_per_tok"]) for r in rows if fam in r["alias"] and r["ms_per_tok"]]
    if len(pts) >= 2:
        a, b = ols(pts); print(f"| {fam} | {L} | {a:.3f} | {1000/a:.0f} | {b:.2f} | {b/L:.3f} ms |")

# --- candidate estimators, scored as mean absolute percentage error of predicted tok/s --------
CANDIDATES = {
    "current 384/GB": lambda gb, l: gb / 0.384,
    "A: GB/0.384 + 3.0 ms flat": lambda gb, l: gb / 0.384 + 3.0,
    "B: GB/0.384 + 0.10 ms x layers": lambda gb, l: gb / 0.384 + 0.10 * (l or 40),
    "C: GB/0.640 + 0.12 ms x layers": lambda gb, l: gb / 0.640 + 0.12 * (l or 40),
    "D: min(384/GB, 300 tok/s)": lambda gb, l: max(gb / 0.384, 1000 / 300),   # ms/token: max of the two times == min of the two rates
}
print("\n| model | MAPE all | MAPE < 1.6 GB | worst (cell) |\n|---|---:|---:|---:|")
signed = {}
for name, f in CANDIDATES.items():
    errs = [((1000 / f(r["gb"], layers(r["alias"]))) / r["meas"] - 1) * 100 for r in rows]
    signed[name] = errs
    small = [e for e, r in zip(errs, rows) if r["gb"] < 1.6]
    w = max(range(len(errs)), key=lambda i: abs(errs[i]))
    print(f"| {name} | {sum(map(abs, errs))/len(errs):.1f}% | {sum(map(abs, small))/len(small):.1f}% | {errs[w]:+.1f}% ({rows[w]['alias']}) |")
print("\nModel C signed error per cell:")
for r, e in sorted(zip(rows, signed["C: GB/0.640 + 0.12 ms x layers"]), key=lambda t: t[0]["gb"]):
    print(f"  {r['alias']:28s} {r['gb']:.3f} GB  {e:+.1f}%")
