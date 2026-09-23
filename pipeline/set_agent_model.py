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
script detects it and falls back to local Qwen 3.8 27B Q6_K. This requires:
  - qwen3.8-27b-q6k-cuda.service to be startable
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

# ── Local fallback tier (Qwen 3.8 27B Q6_K on port 8085) ────
LOCAL_PROVIDER = "local-qwen-q6k"
LOCAL_MODEL = "qwen3.8-27b"
LOCAL_EFFORT = "xhigh"
LOCAL_LLM_SERVICE = "qwen3.8-27b-q6k-cuda.service"
LOCAL_HEALTH_URL = "http://127.0.0.1:8085/health"

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
            # Fallback state expires after 1 hour — retry cloud periodically
            if time.time() - mtime < 3600:
                return True
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
            # Check remaining credits
            limit = data.get("limit")
            usage = data.get("usage")
            if limit is not None and usage is not None:
                remaining = limit - usage
                if remaining <= 0:
                    _mark_cloud_exhausted(f"credits exhausted (used {usage}/{limit})")
                    return False
            _clear_cloud_exhausted()
            return True
        elif r.status_code in (401, 402, 429):
            _mark_cloud_exhausted(f"API returned {r.status_code}")
            return False
    except Exception as e:
        # Network error — don't mark exhausted, just fall back for this call
        print(f"Cloud probe failed: {e}", file=sys.stderr)
        return False

    return True


def _ensure_local_llm():
    """Start local LLM if not running. Also signals run.sh to use VRAM management."""
    try:
        r = None
        if requests:
            r = requests.get(LOCAL_HEALTH_URL, timeout=3)
        if r and r.status_code == 200:
            return True
    except:
        pass

    print("Starting local LLM for fallback...", file=sys.stderr)
    subprocess.run(["systemctl", "start", LOCAL_LLM_SERVICE], capture_output=True)

    for i in range(60):
        try:
            if requests:
                r = requests.get(LOCAL_HEALTH_URL, timeout=3)
                if r.status_code == 200:
                    print(f"Local LLM ready ({i+1}s)", file=sys.stderr)
                    return True
        except:
            pass
        time.sleep(1)

    print("WARNING: Local LLM failed to start in 60s", file=sys.stderr)
    return False


def route_for(preset):
    """Return (provider, model, effort) for a preset.
    Falls back to local Qwen 3.8 27B if cloud is exhausted."""

    cloud_ok = _probe_cloud()

    if cloud_ok:
        if preset in PRO_PRESETS:
            return CLOUD_PROVIDER, PRO_MODEL, PRO_EFFORT
        if preset in VISION_PRESETS:
            return CLOUD_PROVIDER, VISION_MODEL, VISION_EFFORT
        return CLOUD_PROVIDER, FLASH_MODEL, FLASH_EFFORT
    else:
        # Cloud exhausted — fall back to local Qwen 3.8 27B
        _ensure_local_llm()

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
                 "Cloud API exhausted — falling back to local Qwen 3.8 27B Q6_K.\n"
                 "VRAM management active: LLM stops during video generation.",
                 "Cloud Fallback"],
                capture_output=True,
            )

        # Local 27B is multimodal — handles all tiers including vision
        return LOCAL_PROVIDER, LOCAL_MODEL, LOCAL_EFFORT


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
