#!/usr/bin/env python3
"""Per-agent model routing with automatic cloud→local fallback.

Single source of truth for "which model does agent <preset> use". Both run.sh
(via set_agent_model) and pipeline/generate_clip.py call this, so a subprocess
that launches a dsh agent can never inherit a stale route from the previous
agent call.

Usage:
    set_agent_model.py <preset> [--show]

Rewrites the `agent-default-model` section of /root/.dsh/settings.yaml
atomically (write temp + os.replace) so a killed run can never leave a
truncated settings.yaml.

FALLBACK: If the cloud provider returns 402/429 (budget/rate limit), the
script detects it and falls back to local NInfer (ninfer :8080, then
ninfer-us :8081, then qwen q6k :8085 — first healthy wins). This requires:
  - one of those services to be startable (ninfer/ninfer-us Conflicts= each other)
  - CLOUD_ROUTING in run.sh switches to 0 for VRAM management

NOTE: dsh resolves the model from this section AT LAUNCH, so it must be
rewritten before every dsh call. The `engine:` field in presets is a no-op.
"""
import os
import re
import subprocess
import sys
import tempfile
import time

try:
    import requests
except ImportError:
    requests = None

SETTINGS = os.environ.get("DSH_SETTINGS", "/root/.dsh/settings.yaml")

# ── Cloud tier (OpenRouter) ──────────────────────────────────
CLOUD_PROVIDER = "openrouter"

# Reasoning tier — heavy text-only thinking.
PRO_MODEL = "deepseek/deepseek-v4-pro"
PRO_EFFORT = "high"

# Vision tier — qa-inspector needs to see pixels.
VISION_MODEL = "google/gemini-3-flash-preview"
VISION_EFFORT = "high"

# Mechanical tier — drives ComfyUI / TTS / ffmpeg and writes JSON.
FLASH_MODEL = "deepseek/deepseek-v4.1-flash"
FLASH_EFFORT = "low"

# Budget fallback for vision (6x cheaper)
VISION_MODEL_BUDGET = "qwen/qwen3-vl-32b-instruct"

# ── Local fallback tier (NInfer, 262k KV) ──────────────────
# Prefer whichever ninfer unit is actually healthy; both are registered
# as dsh providers. systemd Conflicts= means only one runs at a time.
LOCAL_PROVIDER = "ninfer"
LOCAL_MODEL = "qwen3.8-27b"
LOCAL_EFFORT = "xhigh"
LOCAL_ALTS = (
    ("ninfer", "qwen3.8-27b", "ninfer.service", "http://127.0.0.1:8080/health"),
    (
        "ninfer-us",
        "qwen38-huihui-abliterated-ninfer-nvfp4",
        "ninfer-us.service",
        "http://127.0.0.1:8081/health",
    ),
    ("local-qwen-q6k", "qwen3.8-27b", "qwen3.8-27b-q6k-cuda.service", "http://127.0.0.1:8085/health"),
)

PRO_PRESETS = {
    "story-creator",
    "director",
    "screenplay-reviewer",
    "character-designer",
    "location-designer",
}
VISION_PRESETS = {
    "qa-inspector",
}

# State file to track fallback mode across calls
FALLBACK_STATE = "/root/production_pack/output/.cloud_fallback_state"


def _is_cloud_exhausted():
    """Check if we've already detected cloud exhaustion this session."""
    if os.path.exists(FALLBACK_STATE):
        try:
            mtime = os.path.getmtime(FALLBACK_STATE)
            # Fallback state persists for the whole run (24h) — OpenRouter
            # monthly credits don't refill mid-run, and a short TTL caused
            # the route to bounce back to cloud after 1h and 402 again.
            # Manual clear: delete .cloud_fallback_state + .use_local_llm.
            if time.time() - mtime < 86400:
                return True
        except:
            pass
        # Expired/stale — clear so a future explicit cloud retry works
        try:
            os.remove(FALLBACK_STATE)
        except:
            pass
    return False


def _mark_cloud_exhausted(reason):
    """Mark cloud as exhausted so subsequent calls skip the probe."""
    os.makedirs(os.path.dirname(FALLBACK_STATE), exist_ok=True)
    with open(FALLBACK_STATE, "w") as f:
        f.write(f"{reason}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    print(f"CLOUD EXHAUSTED: {reason} — switching to local Qwen 3.8 27B", file=sys.stderr)


def _clear_cloud_exhausted():
    """Clear fallback state (cloud is working again)."""
    if os.path.exists(FALLBACK_STATE):
        os.remove(FALLBACK_STATE)


def _probe_cloud():
    """Quick probe to check if cloud API is accepting requests.
    Returns True if cloud is available, False if exhausted/down."""
    if _is_cloud_exhausted():
        return False

    if not requests:
        return True  # can't probe, assume OK

    # Read OpenRouter API key from dsh settings or env
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        env_file = "/root/.hermes/.env"
        if os.path.exists(env_file):
            for line in open(env_file):
                if line.startswith("OPENROUTER_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    break

    if not api_key:
        return True  # no key to probe with, assume OK

    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            # Key monthly limit (may differ from OpenRouter account credits —
            # a request can still 402 when key remaining > 0 if account
            # credits can't cover max_tokens; runtime 402 handling in
            # run.sh catches that case via --mark-exhausted).
            limit = data.get("limit")
            usage = data.get("usage")
            limit_remaining = data.get("limit_remaining")
            if limit_remaining is not None:
                remaining = limit_remaining
            elif limit is not None and usage is not None:
                remaining = limit - usage
            else:
                remaining = None
            if remaining is not None and remaining <= 0:
                _mark_cloud_exhausted(f"credits exhausted (remaining={remaining})")
                return False
            # auth/key only reflects the KEY's monthly limit — OpenRouter
            # account credits are checked per-request and can 402 even when
            # key remaining > 0 ("can only afford N tokens" for large
            # max_tokens). Probe with the SAME max_tokens dsh uses so the
            # credit check matches real requests; a 402 here costs nothing.
            try:
                pr = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek/deepseek-v4.1-flash",
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 32768,
                    },
                    timeout=15,
                )
                if pr.status_code in (401, 402, 429):
                    _mark_cloud_exhausted(
                        f"probe completion returned {pr.status_code}: "
                        f"{pr.text[:200]}"
                    )
                    return False
            except Exception as e:
                # Probe POST failed — don't hard-fail, auth/key already said OK
                print(f"Completion probe failed: {e}", file=sys.stderr)
            # DO NOT clear exhausted state here on success alone — if we
            # entered this branch because _is_cloud_exhausted() was false
            # (state expired or never set), clearing is harmless; but if a
            # prior runtime 402 set the state and this probe somehow passes
            # (race, temporary), clearing would bounce us back to cloud.
            # Only clear when the caller explicitly requests a cloud retry.
            return True
        elif r.status_code in (401, 402, 429):
            _mark_cloud_exhausted(f"API returned {r.status_code}")
            return False
    except Exception as e:
        # Network error — don't mark exhausted, just fall back for this call
        print(f"Cloud probe failed: {e}", file=sys.stderr)
        return False

    return True


def _health(url):
    if not requests:
        return False
    try:
        r = requests.get(url, timeout=3)
        return r is not None and r.status_code == 200
    except Exception:
        return False


def _pick_local():
    """Return the first healthy (provider, model) local route."""
    for provider, model, _svc, health in LOCAL_ALTS:
        if _health(health):
            return provider, model
    return LOCAL_PROVIDER, LOCAL_MODEL


def _ensure_local_llm():
    """Start a local LLM if none is healthy. Prefer ninfer, then ninfer-us, then q6k."""
    for provider, model, _svc, health in LOCAL_ALTS:
        if _health(health):
            return True, provider, model

    # Nothing healthy — free VRAM then start the preferred service (ninfer).
    print("Restarting ComfyUI to free VRAM before starting local LLM...", file=sys.stderr)
    subprocess.run(["systemctl", "restart", "comfyui.service"], capture_output=True)
    time.sleep(5)
    provider, model, svc, health = LOCAL_ALTS[0]
    print(f"Starting {svc} for fallback...", file=sys.stderr)
    subprocess.run(["systemctl", "start", svc], capture_output=True)

    # Wait on any alt becoming healthy (start of preferred may fail if Conflicts/VRAM).
    for i in range(120):
        for provider, model, svc2, h in LOCAL_ALTS:
            if _health(h):
                print(f"Local LLM ready ({i+1}s): {provider}/{model}", file=sys.stderr)
                return True, provider, model
        time.sleep(1)

    print("WARNING: Local LLM failed to start in 120s", file=sys.stderr)
    return False, LOCAL_PROVIDER, LOCAL_MODEL


def route_for(preset):
    """Return (provider, model, effort) for a preset.
    Falls back to local NInfer if cloud is exhausted."""

    cloud_ok = _probe_cloud()

    if cloud_ok:
        if preset in PRO_PRESETS:
            return CLOUD_PROVIDER, PRO_MODEL, PRO_EFFORT
        if preset in VISION_PRESETS:
            return CLOUD_PROVIDER, VISION_MODEL, VISION_EFFORT
        return CLOUD_PROVIDER, FLASH_MODEL, FLASH_EFFORT
    else:
        # Cloud exhausted — fall back to whichever local NInfer/Qwen is healthy
        _ok, local_provider, local_model = _ensure_local_llm()

        # Signal run.sh and generate_clip.py to use VRAM management
        os.environ["CLOUD_ROUTING"] = "0"

        # Write signal file so run.sh detects fallback across subprocesses
        signal_file = "/root/production_pack/output/.use_local_llm"
        if not os.path.exists(signal_file):
            with open(signal_file, "w") as f:
                f.write("cloud_exhausted\n")
            # Notify via Discord
            subprocess.run(
                ["bash", "/root/production_pack/pipeline/notify.sh",
                 "Cloud API exhausted — falling back to local "
                 f"{local_provider}/{local_model}.\n"
                 "VRAM management active: LLM stops during video generation.",
                 "Cloud Fallback"],
                capture_output=True,
            )

        # Local models handle all tiers including vision (text+image providers)
        return local_provider, local_model, LOCAL_EFFORT


def rewrite(provider, model, effort, path=SETTINGS):
    text = open(path).read()
    m = re.search(r"(?m)^agent-default-model:[ \t]*$", text)
    if not m:
        sys.exit("agent-default-model section not found in " + path)
    head, tail = text[: m.start()], text[m.end():]

    # Drop the section body: everything until the next line that starts at
    # column 0. Blank lines and indented/comment lines belong to this section.
    # Test with .strip() — a line of just "\n" has first char "\n", not '',
    # so a naive "first column-0 line" test treats blanks as content and
    # duplicates the section instead of replacing it (YAML dup keys = last
    # wins, so that bug looks like a successful write with no effect).
    lines = tail.splitlines(keepends=True)
    rest = ""
    for i, ln in enumerate(lines):
        if ln.strip() and ln[:1] not in (" ", "\t", "#"):
            rest = "".join(lines[i:])
            break

    block = (
        "agent-default-model:\n"
        "  # Managed by production_pack/pipeline/set_agent_model.py — do not hand-edit.\n"
        f"  provider: {provider}\n"
        f"  model: {model}\n"
        f"  reasoningEffort: {effort}\n"
    )
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".settings-", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(head + block + rest)
    os.replace(tmp, path)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    # External trigger: mark cloud exhausted without rewriting a route.
    # Used by run.sh when a live dsh call returns PI_AI_ERROR 402/429 —
    # the pre-launch probe can miss account-credit exhaustion that only
    # surfaces on the actual completion request.
    if sys.argv[1] == "--mark-exhausted":
        reason = sys.argv[2] if len(sys.argv) > 2 else "runtime 402/429 from dsh"
        _mark_cloud_exhausted(reason)
        # Also write the persistent signal so run.sh flips CLOUD_ROUTING
        # for VRAM management across subprocesses / future resumes.
        signal_file = "/root/production_pack/output/.use_local_llm"
        os.makedirs(os.path.dirname(signal_file), exist_ok=True)
        if not os.path.exists(signal_file):
            with open(signal_file, "w") as f:
                f.write(f"{reason}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        return

    preset = sys.argv[1]
    provider, model, effort = route_for(preset)

    if "--show" in sys.argv:
        print(f"{preset} -> {provider}/{model} (effort={effort})")
        return

    rewrite(provider, model, effort)

    # Verify: YAML must parse, and there must be exactly one
    # agent-default-model section carrying the values we just wrote.
    try:
        import yaml
        with open(SETTINGS) as fh:
            data = yaml.safe_load(fh)
        got = data.get("agent-default-model") or {}
        if got.get("provider") != provider or got.get("model") != model:
            sys.exit(
                f"settings.yaml verify FAILED: expected {provider}/{model}, "
                f"got {got.get('provider')}/{got.get('model')}"
            )
        nprov = len(data.get("llm-pi-ai", {}).get("providers", {}))
    except ImportError:
        nprov = -1

    if open(SETTINGS).read().count("\nagent-default-model:") != 1:
        sys.exit("settings.yaml verify FAILED: duplicate agent-default-model sections")

    print(f"{preset} -> {provider}/{model} (effort={effort}) [providers={nprov}]")


if __name__ == "__main__":
    main()
