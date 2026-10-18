#!/usr/bin/env python3
"""Per-run ledger of cloud LLM calls, with per-provider caps.

Every dsh agent launch in run.sh is bracketed by

    cloud_ledger.py check  <run_dir> --provider P              (before — exit 1 = cap reached → halt)
    cloud_ledger.py record <run_dir> --preset --provider --model --effort --step
                           --prompt-file --raw-file --stderr-file --exit N --seconds S
    cloud_ledger.py summary <run_dir>

Ledger: <run_dir>/cloud_usage.json

    {"caps": {"anthropic": {"calls": 12, "usd_est": 9.0}, "opencode-go-deepseek": {"calls": 1500}},
     "calls": [{"ts", "preset", "provider", "model", "effort", "step",
                "prompt_chars", "output_chars", "input_tokens_est", "output_tokens_est",
                "input_tokens", "output_tokens", "usd_est", "exit", "seconds"}],
     "totals": {"calls": n, "usd_est": x, "by_provider": {P: {"calls", "usd_est",
                "input_tokens_est", "output_tokens_est"}}}}

Token counts are estimated at 4 characters per token unless dsh printed real
usage on stderr (`input_tokens`/`output_tokens`, any JSON-ish form — parsed when
found). Prices are USD per million tokens (input, output); OpenCode Go is a
prepaid pool and is costed at 0 but still call-capped.

Caps come from the environment at `check` time:
    CLOUD_MAX_CALLS_ANTHROPIC   (default 12)    CLOUD_MAX_USD_EST_ANTHROPIC (default 9)
    CLOUD_MAX_CALLS_OPENCODE    (default 1500)
Notifications at 50 % / 80 % are the caller's job (summary prints the ratios).
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
    "gemini-3.8-flash": (0.0, 0.0),
    "deepseek-v4-flash-vision-exp": (0.0, 0.0),
}
CHARS_PER_TOKEN = 4.0


def _ledger_path(run_dir):
    return os.path.join(run_dir, "cloud_usage.json")


def _caps_from_env():
    def num(name, default, cast=int):
        v = os.environ.get(name)
        try:
            return cast(v) if v not in (None, "") else default
        except ValueError:
            return default
    anth = {"calls": num("CLOUD_MAX_CALLS_ANTHROPIC", 12),
            "usd_est": num("CLOUD_MAX_USD_EST_ANTHROPIC", 9.0, float)}
    oc = {"calls": num("CLOUD_MAX_CALLS_OPENCODE", 1500)}
    return {"anthropic": anth,
            "opencode-go-deepseek": oc, "opencode-go-deepseek-pro": oc, "opencode-go": oc}


def load(run_dir):
    p = _ledger_path(run_dir)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return {"caps": _caps_from_env(), "calls": [], "totals": {"calls": 0, "usd_est": 0.0, "by_provider": {}}}


def save(run_dir, data):
    p = _ledger_path(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=run_dir, suffix=".tmp", prefix=".cloud_usage")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, p)


def _retotal(data):
    by = {}
    usd = 0.0
    for c in data["calls"]:
        d = by.setdefault(c["provider"], {"calls": 0, "usd_est": 0.0,
                                          "input_tokens_est": 0, "output_tokens_est": 0})
        d["calls"] += 1
        d["usd_est"] += c.get("usd_est", 0.0)
        d["input_tokens_est"] += c.get("input_tokens") or c.get("input_tokens_est", 0)
        d["output_tokens_est"] += c.get("output_tokens") or c.get("output_tokens_est", 0)
        usd += c.get("usd_est", 0.0)
    data["totals"] = {"calls": len(data["calls"]), "usd_est": round(usd, 4), "by_provider": by}


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


def cmd_check(args):
    data = load(args.run_dir)
    data["caps"] = _caps_from_env()
    _retotal(data)
    cap = data["caps"].get(args.provider)
    if cap is None:
        print(f"[ledger] no cap configured for {args.provider}; allowed")
        save(args.run_dir, data)
        return 0
    used = data["totals"]["by_provider"].get(args.provider, {"calls": 0, "usd_est": 0.0})
    for key, label in (("calls", "calls"), ("usd_est", "usd_est")):
        if key in cap and cap[key] is not None and used.get(key, 0) >= cap[key]:
            print(f"[ledger] CAP REACHED for {args.provider}: {label} {used.get(key)} >= {cap[key]}")
            save(args.run_dir, data)
            return 1
    ratios = []
    for key in ("calls", "usd_est"):
        if key in cap and cap[key]:
            ratios.append(f"{key} {used.get(key, 0):.4g}/{cap[key]:.4g}")
    print(f"[ledger] {args.provider} ok ({', '.join(ratios)})")
    save(args.run_dir, data)
    return 0


def cmd_record(args):
    data = load(args.run_dir)
    prompt_chars = _size(args.prompt_file) if args.prompt_file else 0
    output_chars = _size(args.raw_file) if args.raw_file else 0
    real_in, real_out = _real_usage(args.stderr_file) if args.stderr_file else (None, None)
    in_est = int(prompt_chars / CHARS_PER_TOKEN)
    out_est = int(output_chars / CHARS_PER_TOKEN)
    price_in, price_out = PRICES_USD_PER_M.get(args.model, (0.0, 0.0))
    tin = real_in if real_in is not None else in_est
    tout = real_out if real_out is not None else out_est
    usd = (tin * price_in + tout * price_out) / 1e6
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "preset": args.preset, "provider": args.provider, "model": args.model,
        "effort": args.effort, "step": args.step,
        "prompt_chars": prompt_chars, "output_chars": output_chars,
        "input_tokens_est": in_est, "output_tokens_est": out_est,
        "input_tokens": real_in, "output_tokens": real_out,
        "usd_est": round(usd, 5), "exit": args.exit, "seconds": args.seconds,
    }
    data["calls"].append(entry)
    _retotal(data)
    save(args.run_dir, data)
    tot = data["totals"]["by_provider"].get(args.provider, {})
    print(f"[ledger] {args.preset} on {args.provider}/{args.model}: ~{tin} in / ~{tout} out tokens, "
          f"${usd:.4f}; provider total {tot.get('calls')} calls ${tot.get('usd_est', 0):.3f}")
    return 0


def cmd_summary(args):
    data = load(args.run_dir)
    data["caps"] = data.get("caps") or _caps_from_env()
    _retotal(data)
    print(f"cloud calls: {data['totals']['calls']}  est. USD: {data['totals']['usd_est']:.3f}")
    for prov, d in data["totals"]["by_provider"].items():
        cap = data["caps"].get(prov, {})
        parts = [f"{d['calls']} calls"]
        if cap.get("calls"):
            parts[-1] += f" / cap {cap['calls']} ({100.0 * d['calls'] / cap['calls']:.0f}%)"
        parts.append(f"${d['usd_est']:.3f}")
        if cap.get("usd_est"):
            parts[-1] += f" / cap ${cap['usd_est']:.2f} ({100.0 * d['usd_est'] / cap['usd_est']:.0f}%)"
        parts.append(f"~{d['input_tokens_est']} in / ~{d['output_tokens_est']} out tokens")
        print(f"  {prov}: " + ", ".join(parts))
    if args.json:
        print(json.dumps(data["totals"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("run_dir"); c.add_argument("--provider", required=True)
    r = sub.add_parser("record"); r.add_argument("run_dir")
    for name in ("--preset", "--provider", "--model", "--effort", "--step"):
        r.add_argument(name, default="")
    r.add_argument("--prompt-file"); r.add_argument("--raw-file"); r.add_argument("--stderr-file")
    r.add_argument("--exit", type=int, default=0); r.add_argument("--seconds", type=float, default=0.0)
    s = sub.add_parser("summary"); s.add_argument("run_dir"); s.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    return {"check": cmd_check, "record": cmd_record, "summary": cmd_summary}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
