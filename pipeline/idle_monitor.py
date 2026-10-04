#!/usr/bin/env python3
"""Idle Monitor — watch a TTS/generation service; if it stays idle past a
threshold, hand VRAM back to NInfer and ask the agent to continue debugging.

Designed to run as a one-shot from a systemd oneshot unit (or in a loop):
  1. Probe the watched service's health endpoint.
  2. If it is DOWN or has produced no activity for IDLE_SECONDS, consider it
     "stuck / idle".
  3. On idle: stop the watched service, start ninfer, and write a handoff note
     to output/agent_handoff.log so the next agent run picks up debugging.

Reclamation is deliberately conservative — three guards must ALL pass before a
service is touched:
  a) the unit is `active` (never race `systemctl start` / restart);
  b) unit uptime >= --idle-seconds (never kill a service that is still booting);
  c) probe failures have been continuous for >= --idle-seconds (a single
     blip — e.g. a slow /prompt during a heavy render — must not reclaim).
If the unit is not `active` the monitor only logs: the pipeline owns bringing
its own services up, and stopping/starting here is what used to kill ComfyUI
mid-`ensure_comfyui` wait.

Usage:
  python3 idle_monitor.py --service cosyvoice --port 50000 \
      --health-path / --idle-seconds 120 --log output/idle_monitor.log

Exit codes:
  0  service healthy / active — nothing done
  1  service idle/stuck — ninfer restarted, handoff written
  2  probe error (treated as down)
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

NINFER_URL = "http://127.0.0.1:8080"
PACK_DIR = "/root/production_pack"


def log(msg, log_path):
    line = f"[idle-monitor {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(log_path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def probe(base_url, path, timeout=8):
    """Return (ok: bool, detail: str)."""
    url = base_url.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return (r.status == 200, f"HTTP {r.status}")
    except Exception as e:
        return (False, str(e)[:120])


def systemctl(action, svc):
    subprocess.run(["systemctl", action, svc], capture_output=True, timeout=60)


def unit_uptime(unit):
    """Seconds since the unit entered its active state, or None if unknown."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit],
            capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    # e.g. "Sat 2026-09-26 22:18:38 IST" — parse the wall-clock part only; we
    # compare it against time.time(), so the timezone suffix is irrelevant.
    toks = out.split()[:4]
    if len(toks) < 4:
        return None
    try:
        dt = datetime.datetime.strptime(" ".join(toks), "%a %Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return max(0.0, time.time() - time.mktime(dt))


def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {}


def save_state(path, st):
    try:
        with open(path, "w") as f:
            json.dump(st, f)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", required=True, help="systemd unit name (no .service)")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--health-path", default="/")
    ap.add_argument("--idle-seconds", type=int, default=120)
    ap.add_argument("--log", default=os.path.join(PACK_DIR, "output", "idle_monitor.log"))
    # Which pipeline phase expects this service up: "audio" (TTS engines) or
    # "gen" (ComfyUI image/video). The matching marker file gates reclamation.
    ap.add_argument("--phase", choices=["audio", "gen"], default="audio")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    unit = args.service if args.service.endswith(".service") else f"{args.service}.service"
    base = f"http://127.0.0.1:{args.port}"
    state_path = os.path.join(PACK_DIR, "output", f".idle_monitor_state.{unit}")
    key = "first_fail"

    # Is the unit even running?
    active = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip()
    ok, detail = probe(base, args.health_path)

    if active == "active" and ok:
        if os.path.exists(state_path):
            os.remove(state_path)
        log(f"{unit} healthy ({detail}) — leaving it alone.", args.log)
        sys.exit(0)

    # Guard (a): the unit is not `active`. It is either legitimately off
    # between pipeline phases, or it is mid-`systemctl start`/restart. Either
    # way the pipeline owns its own lifecycle — act on nothing here, or we
    # race `ensure_comfyui` and kill a service the run is waiting on.
    if active != "active":
        if os.path.exists(state_path):
            os.remove(state_path)
        marker = ".audio_phase_active" if args.phase == "audio" else ".gen_phase_active"
        in_expected_phase = os.path.exists(os.path.join(PACK_DIR, "output", marker))
        if in_expected_phase:
            log(f"{unit} is {active} while the {args.phase} phase expects it up — "
                f"leaving lifecycle to the pipeline (probe={detail}).", args.log)
        else:
            log(f"{unit} not running (expected between phases) — no action.", args.log)
        sys.exit(0)

    # Guard (b): unit uptime — never reclaim a service that is still booting.
    uptime = unit_uptime(unit)
    if uptime is not None and uptime < args.idle_seconds:
        log(f"{unit} active for only {uptime:.0f}s (< {args.idle_seconds}s) — "
            f"still starting up, waiting (probe={detail}).", args.log)
        if os.path.exists(state_path):
            os.remove(state_path)
        sys.exit(0)

    # Guard (c): sustained failure — probe failures must be continuous for
    # >= idle_seconds. A single blip during a heavy render is not "idle".
    st = load_state(state_path)
    now = time.time()
    first_fail = st.get(key)
    if not isinstance(first_fail, (int, float)) or first_fail > now:
        st = {key: now, "uptime": uptime, "probe": detail}
        save_state(state_path, st)
        log(f"{unit} active but probe failing ({detail}) — watching for "
            f"{args.idle_seconds}s before reclaiming.", args.log)
        sys.exit(0)
    elapsed = now - first_fail
    if elapsed < args.idle_seconds:
        log(f"{unit} probe failing for {elapsed:.0f}s/{args.idle_seconds}s "
            f"({detail}) — not yet sustained.", args.log)
        sys.exit(0)

    # Reclaim: active, booted, and unresponsive for a sustained window.
    reason = f"active-but-unresponsive for {elapsed:.0f}s, uptime {uptime:.0f}s"
    log(f"{unit} appears idle/stuck ({reason}, probe={detail}) — "
        f"handing VRAM back to NInfer.", args.log)
    if os.path.exists(state_path):
        os.remove(state_path)

    # Stop the stuck service to free VRAM
    systemctl("stop", unit)
    time.sleep(2)

    # Bring NInfer back up (only if it is not already serving)
    n_ok, _ = probe(NINFER_URL, "/health")
    if not n_ok:
        systemctl("start", "ninfer.service")
        for _ in range(120):
            n_ok, _ = probe(NINFER_URL, "/health")
            if n_ok:
                break
            time.sleep(1)
    log(f"NInfer {'up' if n_ok else 'FAILED to start'} on :8080", args.log)

    # Write a handoff note for the next agent run to continue debugging
    phase_label = "audio (TTS)" if args.phase == "audio" else "image/video generation (ComfyUI)"
    handoff = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "service": unit,
        "phase": args.phase,
        "reason": reason,
        "action_taken": f"stopped {unit}, started ninfer.service",
        "instruction": (
            f"The {args.service} service went idle/stuck during the {phase_label} phase and was "
            f"stopped to free VRAM. NInfer is now running. Continue debugging why {args.service} "
            f"did not serve (check journalctl -u {unit}). Once fixed, restart it during the "
            f"{args.phase} phase."
        ),
    }
    handoff_path = os.path.join(PACK_DIR, "output", "agent_handoff.json")
    with open(handoff_path, "w") as f:
        json.dump(handoff, f, indent=2)
    log(f"Handoff written -> {handoff_path}", args.log)
    sys.exit(1)


if __name__ == "__main__":
    main()
