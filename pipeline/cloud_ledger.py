#!/usr/bin/env python3
"""Per-run ledger of cloud LLM calls, with per-TIER caps.

Every dsh agent launch in run.sh is bracketed by

    cloud_ledger.py check  <run_dir> --provider P --tier T        (before — exit 1 = cap reached → halt)
    cloud_ledger.py record <run_dir> --preset --provider --model --effort --tier --step
                           --prompt-file --raw-file --stderr-file --exit N --seconds S
    cloud_ledger.py summary <run_dir>

Ledger: <run_dir>/cloud_usage.json

    {"caps": {"creative": {"calls": 16, "usd_est": 9.0}, "vision": {"calls": 600, "usd_est": 4.0},
              "execution": {"calls": 1500}},
     "calls": [{"ts", "preset", "provider", "model", "effort", "tier", "step",
                "prompt_chars", "output_chars", "input_tokens_est", "output_tokens_est",
                "input_tokens", "output_tokens", "usd_est", "exit", "seconds"}],
     "totals": {"calls": n, "usd_est": x,
                "by_tier": {T: {"calls", "usd_est", "input_tokens_est", "output_tokens_est"}},
                "by_provider": {P: {...same...}}}}

Caps are per tier because the tiers differ by two orders of magnitude in call
count: creative = a handful of Opus calls per film, vision = one Haiku call per
QA pass (hundreds), execution = the per-clip OpenCode Go calls (prepaid pool).

Token counts are estimated at 4 characters per token unless dsh printed real
usage on stderr (`input_tokens`/`output_tokens`, any JSON-ish form). A vision
call reads frames the prompt text does not contain, so when no real usage is
found it is charged a flat QA_IMAGE_TOKENS_EST input tokens (default 30000 ≈
20 frames) on top of the text estimate. Prices are USD per million tokens.

Caps come from the environment at `check` time:
    CLOUD_MAX_CALLS_CREATIVE   (default 16)   CLOUD_MAX_USD_EST_CREATIVE  (default 9)
    CLOUD_MAX_CALLS_VISION     (default 600)  CLOUD_MAX_USD_EST_VISION    (default 4)
    CLOUD_MAX_CALLS_EXECUTION  (default 1500)
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

PRICES_USD_PER_M = {
    # model: (input, output)
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    # OpenCode Go — prepaid plan, no per-token price exposed; counted, not costed
    "deepseek-v4.1-flash": (0.0, 0.0),
    "deepseek-v4-pro": (0.0, 0.0),
    "qwen3.8-flash": (0.0, 0.0),
    "deepseek-v4-flash-vision-exp": (0.0, 0.0),
}
CHARS_PER_TOKEN = 4.0
TIERS = ("creative", "vision", "execution")


def _ledger_path(run_dir):
    return os.path.join(run_dir, "cloud_usage.json")


def _num(name, default, cast=int):
    v = os.environ.get(name)
    try:
        return cast(v) if v not in (None, "") else default
    except ValueError:
        return default


def _caps_from_env():
    return {
        "creative": {"calls": _num("CLOUD_MAX_CALLS_CREATIVE", 16),
                     "usd_est": _num("CLOUD_MAX_USD_EST_CREATIVE", 9.0, float)},
        "vision": {"calls": _num("CLOUD_MAX_CALLS_VISION", 600),
                   "usd_est": _num("CLOUD_MAX_USD_EST_VISION", 4.0, float)},
        "execution": {"calls": _num("CLOUD_MAX_CALLS_EXECUTION", 1500)},
    }


def load(run_dir):
    p = _ledger_path(run_dir)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return {"caps": _caps_from_env(), "calls": [],
            "totals": {"calls": 0, "usd_est": 0.0, "by_tier": {}, "by_provider": {}}}


def save(run_dir, data):
    p = _ledger_path(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=run_dir, suffix=".tmp", prefix=".cloud_usage")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, p)


def _bucket(d, key):
    return d.setdefault(key, {"calls": 0, "usd_est": 0.0, "input_tokens_est": 0, "output_tokens_est": 0})


def _retotal(data):
    by_tier, by_prov = {}, {}
    usd = 0.0
    for c in data["calls"]:
        for b in (_bucket(by_tier, c.get("tier") or "execution"), _bucket(by_prov, c["provider"])):
            b["calls"] += 1
            b["usd_est"] += c.get("usd_est", 0.0)
            b["input_tokens_est"] += c.get("input_tokens") or c.get("input_tokens_est", 0)
            b["output_tokens_est"] += c.get("output_tokens") or c.get("output_tokens_est", 0)
        usd += c.get("usd_est", 0.0)
    for d in (by_tier, by_prov):
        for b in d.values():
            b["usd_est"] = round(b["usd_est"], 4)
    data["totals"] = {"calls": len(data["calls"]), "usd_est": round(usd, 4),
                      "by_tier": by_tier, "by_provider": by_prov}


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


_USAGE_RE = re.compile(r'"?(input_tokens|output_tokens|prompt_tokens|completion_tokens)"?\s*[:=]\s*(\d+)')


def _real_usage(stderr_file):
    """Pick up real token counts if dsh printed any (last occurrence wins)."""
    found = {}
    try:
        with open(stderr_file, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-200000:]
    except OSError:
        return None, None
    for key, val in _USAGE_RE.findall(tail):
        found[key] = int(val)
    inp = found.get("input_tokens", found.get("prompt_tokens"))
    out = found.get("output_tokens", found.get("completion_tokens"))
    return inp, out


def tier_usage(data, tier):
    return data.get("totals", {}).get("by_tier", {}).get(tier, {"calls": 0, "usd_est": 0.0})


def cmd_check(args):
    data = load(args.run_dir)
    data["caps"] = _caps_from_env()
    _retotal(data)
    tier = args.tier if args.tier in TIERS else "execution"
    cap = data["caps"].get(tier, {})
    used = tier_usage(data, tier)
    for key in ("calls", "usd_est"):
        if cap.get(key) is not None and used.get(key, 0) >= cap[key]:
            print(f"[ledger] CAP REACHED for tier {tier} ({args.provider}): {key} {used.get(key)} >= {cap[key]}")
            save(args.run_dir, data)
            return 1
    ratios = [f"{k} {used.get(k, 0):.4g}/{cap[k]:.4g}" for k in ("calls", "usd_est") if cap.get(k)]
    print(f"[ledger] {tier} ({args.provider}) ok ({', '.join(ratios)})")
    save(args.run_dir, data)
    return 0


def cmd_record(args):
    data = load(args.run_dir)
    tier = args.tier if args.tier in TIERS else "execution"
    prompt_chars = _size(args.prompt_file) if args.prompt_file else 0
    output_chars = _size(args.raw_file) if args.raw_file else 0
    real_in, real_out = _real_usage(args.stderr_file) if args.stderr_file else (None, None)
    in_est = int(prompt_chars / CHARS_PER_TOKEN)
    if tier == "vision" and real_in is None:
        in_est += _num("QA_IMAGE_TOKENS_EST", 30000)
    out_est = int(output_chars / CHARS_PER_TOKEN)
    price_in, price_out = PRICES_USD_PER_M.get(args.model, (0.0, 0.0))
    tin = real_in if real_in is not None else in_est
    tout = real_out if real_out is not None else out_est
    usd = (tin * price_in + tout * price_out) / 1e6
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "preset": args.preset, "provider": args.provider, "model": args.model,
        "effort": args.effort, "tier": tier, "step": args.step,
        "prompt_chars": prompt_chars, "output_chars": output_chars,
        "input_tokens_est": in_est, "output_tokens_est": out_est,
        "input_tokens": real_in, "output_tokens": real_out,
        "usd_est": round(usd, 5), "exit": args.exit, "seconds": args.seconds,
    }
    data["calls"].append(entry)
    _retotal(data)
    save(args.run_dir, data)
    tot = tier_usage(data, tier)
    print(f"[ledger] {args.preset} on {args.provider}/{args.model} ({tier}): ~{tin} in / ~{tout} out tokens, "
          f"${usd:.4f}; tier total {tot.get('calls')} calls ${tot.get('usd_est', 0):.3f}")
    return 0


def cmd_summary(args):
    data = load(args.run_dir)
    data["caps"] = data.get("caps") or _caps_from_env()
    _retotal(data)
    print(f"cloud calls: {data['totals']['calls']}  est. USD: {data['totals']['usd_est']:.3f}")
    for tier, d in data["totals"]["by_tier"].items():
        cap = data["caps"].get(tier, {})
        parts = [f"{d['calls']} calls"]
        if cap.get("calls"):
            parts[-1] += f" / cap {cap['calls']} ({100.0 * d['calls'] / cap['calls']:.0f}%)"
        parts.append(f"${d['usd_est']:.3f}")
        if cap.get("usd_est"):
            parts[-1] += f" / cap ${cap['usd_est']:.2f} ({100.0 * d['usd_est'] / cap['usd_est']:.0f}%)"
        parts.append(f"~{d['input_tokens_est']} in / ~{d['output_tokens_est']} out tokens")
        print(f"  {tier}: " + ", ".join(parts))
    for prov, d in data["totals"]["by_provider"].items():
        print(f"  [{prov}] {d['calls']} calls, ${d['usd_est']:.3f}")
    if args.json:
        print(json.dumps(data["totals"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("run_dir"); c.add_argument("--provider", required=True)
    c.add_argument("--tier", default="execution")
    r = sub.add_parser("record"); r.add_argument("run_dir")
    for name in ("--preset", "--provider", "--model", "--effort", "--step"):
        r.add_argument(name, default="")
    r.add_argument("--tier", default="execution")
    r.add_argument("--prompt-file"); r.add_argument("--raw-file"); r.add_argument("--stderr-file")
    r.add_argument("--exit", type=int, default=0); r.add_argument("--seconds", type=float, default=0.0)
    s = sub.add_parser("summary"); s.add_argument("run_dir"); s.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return {"check": cmd_check, "record": cmd_record, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
