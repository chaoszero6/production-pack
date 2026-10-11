#!/usr/bin/env python3
"""local_llm.py — the ONE place (Python side) that knows which local LLM the
pack runs on. Mirrors pipeline/local_llm.sh exactly; change both together.

Current model: Qwen 3.8 Flash Next 125B (Strata Swift 1.5, IQ3_XXS)
  unit   strata-swift-iq3-xxs.service
  API    http://127.0.0.1:8321/v1   (OpenAI-compatible; /v1/models = health)
  model  swift-1.5-iq3_xxs          (dsh provider "strata-swift", TEXT ONLY)
  VRAM   ~31.8 GB of 32.6 GB — nothing else fits on the GPU beside it
  start  ~2 minutes; the unit's TimeoutStartSec is 300
  note   its ExecStartPre stops comfyui.service — start the LLM first, then
         bring ComfyUI back (it idles at ~0.6 GB).

Usage as a CLI (used by the monitor and by shell scripts without the bash lib):
  python3 local_llm.py status | start | stop | restart | wait [timeout]
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

UNIT = os.environ.get("LLM_UNIT", "strata-swift-iq3-xxs.service")
PORT = int(os.environ.get("LLM_PORT", "8321"))
BASE_URL = f"http://127.0.0.1:{PORT}"
HEALTH_URL = f"{BASE_URL}/v1/models"
CHAT_URL = f"{BASE_URL}/v1/chat/completions"
PROVIDER = os.environ.get("LLM_PROVIDER", "strata-swift")
MODEL = os.environ.get("LLM_MODEL", "swift-1.5-iq3_xxs")
EFFORT = os.environ.get("LLM_EFFORT", "xhigh")
VRAM_MB = 31800
START_TIMEOUT = int(os.environ.get("LLM_START_TIMEOUT", "300"))
LEGACY_UNITS = ["ninfer.service", "ninfer-us.service", "ninfer-qwen36-35b.service",
                "qwen3.8-27b-q6k-cuda.service", "qfn-iq3m-cuda.service",
                "llama-qwen35-122b.service"]
LEGACY_DOCKER = "qwen38-27b-q6k"
LEGACY_PORTS = [8080, 8081, 8083, 8085]


def log(msg: str) -> None:
    # A caller may pipe us into `head`; a closed stdout must never abort a start/wait.
    try:
        print(f"[local-llm {time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except (BrokenPipeError, OSError):
        try:
            sys.stderr.write(f"[local-llm] {msg}" + chr(10))
        except Exception:
            pass


def systemctl(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)


def healthy(timeout: float = 4.0) -> bool:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def unit_state() -> str:
    """active | activating | inactive | failed | deactivating | unknown"""
    try:
        return systemctl("is-active", UNIT, timeout=15).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def active() -> bool:
    return unit_state() == "active"


def uptime() -> float | None:
    try:
        ts = systemctl("show", "-p", "ActiveEnterTimestamp", "--value", UNIT, timeout=15).stdout.strip()
        if not ts:
            return None
        epoch = float(subprocess.run(["date", "-d", ts, "+%s"], capture_output=True,
                                     text=True, timeout=15).stdout.strip())
        return time.time() - epoch
    except Exception:
        return None


def stop_legacy() -> None:
    for u in LEGACY_UNITS:
        try:
            systemctl("stop", u, timeout=60)
        except Exception:
            pass
    try:
        subprocess.run(["docker", "stop", LEGACY_DOCKER], capture_output=True, timeout=60)
    except Exception:
        pass


def wait(timeout: int = START_TIMEOUT) -> bool:
    waited = 0
    while waited < timeout:
        if healthy():
            log(f"{UNIT} healthy after {waited}s")
            return True
        st = unit_state()
        if waited > 15 and st not in ("active", "activating"):
            log(f"{UNIT} is {st} — gave up after {waited}s")
            return False
        time.sleep(2)
        waited += 2
    log(f"WARNING: {UNIT} not healthy after {timeout}s")
    return False


def start(timeout: int = START_TIMEOUT) -> bool:
    if healthy():
        return True
    stop_legacy()
    if not active():
        log(f"starting {UNIT} (takes ~2 min: 40 GB of experts + GPU cache)…")
        systemctl("reset-failed", UNIT)
        systemctl("start", UNIT)
    else:
        log(f"{UNIT} active but not answering yet — waiting")
    return wait(timeout)


def stop() -> None:
    log(f"stopping {UNIT} (freeing {VRAM_MB} MB VRAM)")
    systemctl("stop", UNIT)
    stop_legacy()
    time.sleep(3)


def restart(timeout: int = START_TIMEOUT) -> bool:
    log(f"restarting {UNIT}")
    systemctl("stop", UNIT)
    time.sleep(3)
    return start(timeout)


def chat_ok(timeout: float = 60.0) -> bool:
    """A real inference round-trip — /v1/models can answer while the engine is wedged."""
    import json
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "Reply with OK"}],
                       "max_tokens": 4}).encode()
    req = urllib.request.Request(CHAT_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    t = int(argv[2]) if len(argv) > 2 else START_TIMEOUT
    if cmd == "status":
        up = uptime()
        print(f"{UNIT}: state={unit_state()} healthy={healthy()} "
              f"uptime={'%.0fs' % up if up is not None else '-'} api={BASE_URL}/v1 model={MODEL}")
        return 0 if healthy() else 1
    if cmd == "start":
        return 0 if start(t) else 1
    if cmd == "wait":
        return 0 if wait(t) else 1
    if cmd == "stop":
        stop()
        return 0
    if cmd == "restart":
        return 0 if restart(t) else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
