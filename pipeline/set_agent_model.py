#!/usr/bin/env python3
"""Per-agent model routing for the production pack (OpenRouter, all-cloud).

Single source of truth for "which model does agent <preset> use". Both run.sh
(via set_agent_model) and pipeline/generate_clip.py call this, so a subprocess
that launches a dsh agent can never inherit a stale route from the previous
agent call.

Usage:
    set_agent_model.py <preset> [--show]

Rewrites the `agent-default-model` section of /root/.dsh/settings.yaml
atomically (write temp + os.replace) so a killed run can never leave a
truncated settings.yaml.

NOTE: dsh resolves the model from this section AT LAUNCH, so it must be
rewritten before every dsh call. The `engine:` field in presets is a no-op.
"""
import os
import re
import sys
import tempfile

SETTINGS = os.environ.get("DSH_SETTINGS", "/root/.dsh/settings.yaml")

# The one provider every agent routes through.
PROVIDER = "openrouter"

# Reasoning tier — heavy text-only thinking. Verified: this model REJECTS
# image input (HTTP 404 "No endpoints found that support image input"), so it
# must never be given a vision job.
PRO_MODEL = "deepseek/deepseek-v4-pro"
PRO_EFFORT = "high"

# Vision tier — qa-inspector is the ONLY agent that needs to see pixels; it
# extracts frames at 2 FPS and scores hands/face/identity per frame.
VISION_MODEL = "google/gemini-3-flash-preview"
VISION_EFFORT = "high"

# Mechanical tier — drives ComfyUI / TTS / ffmpeg and writes JSON.
FLASH_MODEL = "deepseek/deepseek-v4.1-flash"
FLASH_EFFORT = "low"

PRO_PRESETS = {
    "story-creator",
    "director",
    "screenplay-reviewer",   # runs once per clip — highest leverage agent
    "character-designer",
    "location-designer",
}
VISION_PRESETS = {
    "qa-inspector",
}

# Cheaper alternative for the vision tier (6x cheaper, ~910 tok/frame vs
# ~1100). Swap VISION_MODEL to this if the OpenRouter budget gets tight.
VISION_MODEL_BUDGET = "qwen/qwen3-vl-32b-instruct"


def route_for(preset):
    """Return (provider, model, effort) for a preset."""
    if preset in PRO_PRESETS:
        return PROVIDER, PRO_MODEL, PRO_EFFORT
    if preset in VISION_PRESETS:
        return PROVIDER, VISION_MODEL, VISION_EFFORT
    return PROVIDER, FLASH_MODEL, FLASH_EFFORT


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
