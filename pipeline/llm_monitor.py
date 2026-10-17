#!/usr/bin/env python3
"""llm_monitor.py — crash / hang watchdog for the local LLM (Strata Swift).

Runs as one pass per invocation from tts-idle-monitor.service (every 30 s).
It answers one question: should the LLM be up right now, and is it?

  LLM should be UP  = no generation phase is active
                      (no output/.gen_phase_active, no output/.audio_phase_active,
                       no ComfyUI job in flight, no llm-parked marker from a
                       pipeline script that deliberately stopped it)
  LLM should be DOWN = any of those hold — the pipeline parked it on purpose so
                      ComfyUI / Orpheus can have the GPU. Never start it then:
                      its ExecStartPre would stop ComfyUI mid-render.

Decisions (all logged to output/llm_monitor.log):
  unit failed/inactive + should be up            → systemctl reset-failed + start,
                                                   wait ≤ 300 s, log + handoff
  unit active, probe failing ≥ --hang-seconds     → restart (engine wedged)
  unit active ≤ --boot-grace s                    → wait (still loading 40 GB)
  unit active, /v1/models ok, chat round-trip
  failing ≥ --hang-seconds (every Nth pass)       → restart (API up, engine dead)
  VRAM squeezed by a stray process while the LLM
  should be up (free < 1 GB, LLM not loaded)      → stop ComfyUI models (/free),
                                                   stop legacy units, then start

Guards: never act while systemd reports "activating"/"deactivating"; a failure
must be sustained across passes (state file) before anything restarts; at most
--max-restarts restarts per hour, after which it only logs + writes the handoff.

Exit codes: 0 nothing to do, 1 action taken, 2 could not recover (handoff written)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_llm as llm  # noqa: E402

PACK_DIR = os.environ.get("PACK_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(PACK_DIR, "output")
MARKERS = (".gen_phase_active", ".audio_phase_active", ".llm_parked")
COMFY = "http://127.0.0.1:8188"


def log(msg: str, path: str) -> None:
    line = f"[llm-monitor {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load(path: str) -> dict:
    try:
        return json.load(open(path))
    except Exception:
        return {}


def save(path: str, st: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(st, open(path, "w"))
    except Exception:
        pass


def comfy_busy() -> bool:
    """True if ComfyUI is serving a job (never pull the GPU from under a render)."""
    try:
        with urllib.request.urlopen(f"{COMFY}/queue", timeout=4) as r:
            q = json.load(r)
        return bool(q.get("queue_running")) or bool(q.get("queue_pending"))
    except Exception:
        return False


def comfy_free_models() -> None:
    try:
        req = urllib.request.Request(f"{COMFY}/free", data=b'{"unload_models":true,"free_memory":true}',
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def gpu_free_mib() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


OTHER_GPU_OWNERS = ("comfyui.service", "tts-cluster.service", "orpheus-backend.service",
                    "chatterbox-tts.service", "cosyvoice.service", "kokoro-tts.service")


def other_owner_active() -> str | None:
    """A unit from another exclusive GPU set is active → the LLM must stay down.
    Starting it would make systemd (Conflicts=) stop that render/synthesis."""
    for u in OTHER_GPU_OWNERS:
        try:
            st = subprocess.run(["systemctl", "is-active", u], capture_output=True, text=True,
                                timeout=15).stdout.strip()
        except Exception:
            continue
        if st in ("active", "activating"):
            return u
    return None


MARKER_MAX_AGE_S = 3 * 3600


def pipeline_alive() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", r"(^|/)(run\.sh|studio\.py|generate_clip\.py|generate_frames\.py|"
                                            r"tts_render\.py|generate_dialogue_audio\.py|upscale_(clip|film)\.py|"
                                            r"generate_(character_sheets|location_refs)\.py)"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except Exception:
        return True  # cannot tell — be conservative


def pipeline_parked_llm() -> str | None:
    """A park marker counts only while it is fresh AND a pipeline process is alive;
    a marker left behind by a crashed run (seen: .audio_phase_active from 2026-10-07)
    would otherwise keep the LLM down forever. Live GPU owners are checked regardless."""
    alive = None
    for m in MARKERS:
        path = os.path.join(OUT, m)
        if not os.path.exists(path):
            continue
        age = time.time() - os.path.getmtime(path)
        if alive is None:
            alive = pipeline_alive()
        if age <= MARKER_MAX_AGE_S and alive:
            return m
        try:
            os.remove(path)
            print(f"[llm-monitor] removed stale marker {m} (age {age / 60:.0f} min, pipeline alive={alive})", flush=True)
        except OSError:
            pass
    return other_owner_active()


def write_handoff(reason: str, action: str, instruction: str) -> str:
    path = os.path.join(OUT, "agent_handoff.json")
    try:
        os.makedirs(OUT, exist_ok=True)
        json.dump({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "service": llm.UNIT, "phase": "llm",
                   "reason": reason, "action_taken": action, "instruction": instruction},
                  open(path, "w"), indent=2)
    except Exception:
        pass
    return path


def journal_tail(n: int = 8) -> str:
    try:
        out = subprocess.run(["journalctl", "-u", llm.UNIT, "-n", str(n), "--no-pager", "-o", "cat"],
                             capture_output=True, text=True, timeout=15).stdout
        return " | ".join(l.strip() for l in out.splitlines() if l.strip())[-600:]
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=os.path.join(OUT, "llm_monitor.log"))
    ap.add_argument("--hang-seconds", type=int, default=120,
                    help="probe must fail continuously this long before a restart")
    ap.add_argument("--boot-grace", type=int, default=330,
                    help="never touch a unit younger than this (startup takes ~2 min, cap 300)")
    ap.add_argument("--chat-every", type=int, default=10,
                    help="run a real chat round-trip every N passes (0 = never)")
    ap.add_argument("--max-restarts", type=int, default=4, help="per rolling hour")
    ap.add_argument("--start-timeout", type=int, default=llm.START_TIMEOUT)
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    state_path = os.path.join(OUT, ".llm_monitor_state.json")
    st = load(state_path)
    now = time.time()
    st["passes"] = int(st.get("passes", 0)) + 1
    restarts = [t for t in st.get("restarts", []) if now - t < 3600]
    st["restarts"] = restarts

    state = llm.unit_state()
    ok = llm.healthy()
    parked = pipeline_parked_llm()
    busy = comfy_busy()
    should_be_up = parked is None and not busy

    # ── transitional states: never race systemd ──
    if state in ("activating", "deactivating"):
        log(f"{llm.UNIT} {state} — waiting", args.log)
        st.pop("first_fail", None)
        save(state_path, st)
        return 0

    # ── healthy ──
    if state == "active" and ok:
        st.pop("first_fail", None)
        # Deep check: the API can answer /v1/models while the engine is wedged.
        if args.chat_every and st["passes"] % args.chat_every == 0:
            if llm.chat_ok(timeout=90):
                st.pop("chat_fail_since", None)
                log(f"{llm.UNIT} healthy (chat round-trip ok)", args.log)
            else:
                since = st.get("chat_fail_since") or now
                st["chat_fail_since"] = since
                if now - since >= args.hang_seconds and (llm.uptime() or 0) > args.boot_grace:
                    log(f"{llm.UNIT} answers /v1/models but chat has failed for {now - since:.0f}s — "
                        f"engine wedged, restarting", args.log)
                    return _restart(st, state_path, args, "engine wedged (chat timeouts)")
                log(f"{llm.UNIT} chat round-trip failing ({now - since:.0f}s/{args.hang_seconds}s)", args.log)
        else:
            log(f"{llm.UNIT} healthy — leaving it alone.", args.log)
        save(state_path, st)
        return 0

    # ── down ──
    if state != "active":
        st.pop("first_fail", None)
        if not should_be_up:
            why = (f"marker {parked}" if parked in MARKERS else f"{parked} owns the GPU") if parked \
                else "ComfyUI job in flight"
            log(f"{llm.UNIT} {state} — not starting ({why}).", args.log)
            save(state_path, st)
            return 0
        # Optional: a stray process holding the GPU would make the start OOM.
        free = gpu_free_mib()
        if free is not None and free < llm.VRAM_MB + 500:
            log(f"only {free} MiB free before start — releasing ComfyUI models + legacy units", args.log)
            comfy_free_models()
            llm.stop_legacy()
            time.sleep(3)
        if len(restarts) >= args.max_restarts:
            path = write_handoff(f"unit {state}; {len(restarts)} restarts in the last hour",
                                 "none (restart budget exhausted)",
                                 f"{llm.UNIT} keeps dying: {journal_tail()}. Check journalctl -u {llm.UNIT}, "
                                 f"free RAM (needs ~40 GB) and VRAM, then systemctl start it.")
            log(f"{llm.UNIT} {state} but restart budget exhausted — handoff {path}", args.log)
            save(state_path, st)
            return 2
        log(f"{llm.UNIT} is {state} while it should be serving — starting (≈2 min)", args.log)
        restarts.append(now)
        st["restarts"] = restarts
        save(state_path, st)
        if llm.start(args.start_timeout):
            write_handoff(f"unit was {state}", "started", "LLM was down and was restarted by llm_monitor; "
                          "agent calls that failed in that window must be retried.")
            log(f"{llm.UNIT} recovered", args.log)
            return 1
        write_handoff(f"unit was {state}", "start FAILED", f"journal: {journal_tail()}")
        log(f"{llm.UNIT} did not come back within {args.start_timeout}s", args.log)
        return 2

    # ── active but not answering ──
    up = llm.uptime()
    if up is not None and up < args.boot_grace:
        log(f"{llm.UNIT} active for {up:.0f}s, still loading (grace {args.boot_grace}s)", args.log)
        st.pop("first_fail", None)
        save(state_path, st)
        return 0
    first = st.get("first_fail")
    if not isinstance(first, (int, float)) or first > now:
        st["first_fail"] = now
        save(state_path, st)
        log(f"{llm.UNIT} active but /v1/models not answering — watching {args.hang_seconds}s", args.log)
        return 0
    if now - first < args.hang_seconds:
        log(f"{llm.UNIT} unresponsive for {now - first:.0f}s/{args.hang_seconds}s", args.log)
        save(state_path, st)
        return 0
    return _restart(st, state_path, args, f"active but unresponsive for {now - first:.0f}s")


def _restart(st: dict, state_path: str, args, reason: str) -> int:
    now = time.time()
    st.pop("first_fail", None)
    st.pop("chat_fail_since", None)
    restarts = [t for t in st.get("restarts", []) if now - t < 3600]
    if len(restarts) >= args.max_restarts:
        path = write_handoff(reason, "none (restart budget exhausted)",
                             f"{llm.UNIT} hangs repeatedly: {journal_tail()}")
        log(f"{reason} but restart budget exhausted — handoff {path}", args.log)
        save(state_path, st)
        return 2
    restarts.append(now)
    st["restarts"] = restarts
    save(state_path, st)
    log(f"{reason} — restarting {llm.UNIT}", args.log)
    if llm.restart(args.start_timeout):
        write_handoff(reason, "restarted", "LLM hung and was restarted by llm_monitor; retry the failed agent call.")
        log(f"{llm.UNIT} back after restart", args.log)
        return 1
    write_handoff(reason, "restart FAILED", f"journal: {journal_tail()}")
    log(f"{llm.UNIT} did not recover after restart", args.log)
    return 2


if __name__ == "__main__":
    sys.exit(main())
