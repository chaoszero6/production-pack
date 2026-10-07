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

STARTUP CONFIGURATION — all tiers overridable via environment variables:
    export DSH_REASONING_PROVIDER=openrouter          # defaults to openrouter
    export DSH_REASONING_MODEL="deepseek/deepseek-v4-pro"
    export DSH_REASONING_EFFORT=high

    export DSH_EXECUTION_PROVIDER=openrouter
    export DSH_EXECUTION_MODEL="deepseek/deepseek-v4.1-flash"
    export DSH_EXECUTION_EFFORT=low

    export DSH_VISION_PROVIDER=openrouter
    export DSH_VISION_MODEL="google/gemini-3-flash-preview"
    export DSH_VISION_EFFORT=high

Provider names must match registered providers in your DSH profiles.
Model identifiers must also be registered under that provider.
Leave blank to use built-in defaults below.

FALLBACK: If the cloud provider returns 402/429, the script detects it
and falls back to local NInfer. Requires CLOUD_ROUTING=0 in run.sh.

NOTE: dsh resolves the model from agent-default-model AT LAUNCH, so it
must be rewritten before every dsh call.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import yaml

try:
    import requests
except ImportError:
    requests = None

SETTINGS = os.environ.get("DSH_SETTINGS", "/root/.dsh/settings.yaml")


# ── Startup Configuration helpers ────────────────────────────
def _env(name, default=None):
    """Read an env var; return default if unset or empty."""
    val = os.environ.get(name)
    return val.strip() if val else default


# ── Defaults per (provider, tier) → (model_id, effort)
_DEFAULTS = {
    # ── Token Harbor (cloud) — primary reasoning provider ────────────────
    ("tokenharbor",    "reasoning"):  ("deepseek-v4-pro",                    "high"),
    ("tokenharbor",    "execution"):  ("deepseek-v4.1-flash",                "low"),
    ("tokenharbor",    "vision"):     ("gemini-3.8-flash",                   "high"),
    # ── OpenRouter — cloud fallback ──────────────────────────────────────
    ("openrouter",     "reasoning"):  ("deepseek/deepseek-v4-pro",           "high"),
    ("openrouter",     "execution"):  ("qwen/qwen3.6-plus",          "low"),
    ("openrouter",     "vision"):     ("google/gemini-3-flash-preview",      "high"),
    # ── OpenCode Go — deepseek fallback ─────────────────────────────────
    ("opencode-go-deepseek-pro", "reasoning"):  ("deepseek-v4-pro",          "max"),
    ("opencode-go-deepseek",     "execution"):  ("deepseek-v4.1-flash",      "max"),
    ("opencode-go-deepseek",     "vision"):     ("gemini-3.8-flash",         "high"),
    # ── Local ninfer-qwen36-35b — primary execution & vision provider ────
    ("ninfer-qwen36-35b",  "execution"):  ("qwen3.6-35b-a3b",                "xhigh"),
    ("ninfer-qwen36-35b",  "vision"):     ("qwen3.6-35b-a3b",                "xhigh"),
    # ── Local Qwen 3.8 27B — primary provider for all tiers ────────────────
    ("ninfer",         "reasoning"):  ("qwen3.8-27b",                        "xhigh"),
    ("ninfer",         "execution"):  ("qwen3.8-27b",                        "xhigh"),
    ("ninfer",         "vision"):     ("qwen3.8-27b",                        "xhigh"),
    ("ninfer-us",      "reasoning"):  ("qwen38-huihui-abliterated-ninfer-nvfp4", "xhigh"),
    ("ninfer-us",      "execution"):  ("qwen38-huihui-abliterated-ninfer-nvfp4", "xhigh"),
}


# Read env vars at import time
_REASONING_LOCAL_DEFAULT = "ninfer"            # Local Qwen 3.8 27B for reasoning
_EXECUTION_LOCAL_DEFAULT = "ninfer"            # Local Qwen 3.8 27B for execution/vision
_CLOUD_DEFAULT           = "openrouter"        # fallback base

REASONING_PROVIDER   = _env("DSH_REASONING_PROVIDER",   _REASONING_LOCAL_DEFAULT)
REASONING_MODEL_ENV  = _env("DSH_REASONING_MODEL",       None)
REASONING_EFFORT_ENV = _env("DSH_REASONING_EFFORT",      None)

EXECUTION_PROVIDER   = _env("DSH_EXECUTION_PROVIDER",   _EXECUTION_LOCAL_DEFAULT)
EXECUTION_MODEL_ENV  = _env("DSH_EXECUTION_MODEL",       None)
EXECUTION_EFFORT_ENV = _env("DSH_EXECUTION_EFFORT",      None)

VISION_PROVIDER      = _env("DSH_VISION_PROVIDER",      _EXECUTION_LOCAL_DEFAULT)
VISION_MODEL_ENV     = _env("DSH_VISION_MODEL",          None)
VISION_EFFORT_ENV    = _env("DSH_VISION_EFFORT",         None)

VISION_MODEL_BUDGET = _env("DSH_VISION_BUDGET_MODEL",    None)

# Map each tier to its module-level provider variable (used when no env var overrides)
_DEFAULT_TIER_DEFAULTS = {
    "reasoning":  REASONING_PROVIDER,
    "execution":  EXECUTION_PROVIDER,
    "vision":     VISION_PROVIDER,
}


def _lookup(tier, provider_env_var=None, model_env_var=None, effort_env_var=None):
    """Resolve (provider_name, model_id, effort) for a given tier.

    Priority:
      1. Explicit *_MODEL env var  → use that model with resolved provider
      2. Default table lookup      → use provider default for this tier
      3. Fallback                  → find any default for this tier
    """
    explicit_model = _env(model_env_var) if model_env_var else None
    explicit_effort = _env(effort_env_var) if effort_env_var else None
    effective_provider = _env(provider_env_var) if provider_env_var else None
    # If no env var is set, fall back to the module-level configured provider
    if effective_provider is None and _DEFAULT_TIER_DEFAULTS.get(tier):
        effective_provider = _DEFAULT_TIER_DEFAULTS[tier]

    # Case 1: explicit model overrides everything
    if explicit_model:
        return effective_provider or _CLOUD_DEFAULT, explicit_model, explicit_effort or _guess_default_effort(tier)

    # Case 2: look up in defaults table using configured provider
    key = (effective_provider, tier)
    if key in _DEFAULTS:
        model, default_effort = _DEFAULTS[key]
        return effective_provider, model, explicit_effort or default_effort

    # Case 3: find any default for this tier
    for (pkey, t), (m, eff) in _DEFAULTS.items():
        if t == tier:
            return pkey, m, explicit_effort or eff

    raise ValueError(f"No routing configured for tier={tier}, no defaults found. "
                     f"Set DSH_{tier.upper()}_PROVIDER and/or DSH_{tier.upper()}_MODEL.")


def _guess_default_effort(tier):
    """Fallback effort guess when no env var or default exists."""
    if tier == "reasoning":
        return "high"
    elif tier == "execution":
        return "low"
    elif tier == "vision":
        return "high"
    return "high"


def route_for(preset):
    """Return (provider, model, effort) for the given preset."""
    if preset in PRO_PRESETS:
        return _lookup(
            "reasoning", "DSH_REASONING_PROVIDER",
            "DSH_REASONING_MODEL", "DSH_REASONING_EFFORT"
        )
    if preset in VISION_PRESETS:
        return _lookup(
            "vision", "DSH_VISION_PROVIDER",
            "DSH_VISION_MODEL", "DSH_VISION_EFFORT"
        )
    return _lookup(
        "execution", "DSH_EXECUTION_PROVIDER",
        "DSH_EXECUTION_MODEL", "DSH_EXECUTION_EFFORT"
    )


# ── Preset groups ────────────────────────────────────────────
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

# Local fallback tier — NInfer services for CLOUD_ROUTING=0 mode
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

# State file to track fallback mode across calls
FALLBACK_STATE = os.path.join(
    os.environ.get("PACK_DIR")
    or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output",
    ".cloud_fallback_state",
)


def _is_cloud_exhausted():
    """Check if we've already detected cloud exhaustion this session."""
    if os.path.exists(FALLBACK_STATE):
        try:
            mtime = os.path.getmtime(FALLBACK_STATE)
            # Persist for 24h — monthly credits don't refill mid-run.
            if time.time() - mtime < 86400:
                return True
        except Exception:
            pass
        try:
            os.remove(FALLBACK_STATE)
        except Exception:
            pass
    return False


def _mark_cloud_exhausted(reason):
    """Persist cloud exhaustion state so future calls skip cloud immediately."""
    try:
        os.makedirs(os.path.dirname(FALLBACK_STATE), exist_ok=True)
        with open(FALLBACK_STATE, "w") as fh:
            fh.write(f"{time.time()}\n{reason}\n")
    except Exception:
        pass


# ── Try cloud route, fallback on error ───────────────────────

def _try_cloud_route(provider, model, effort):
    """Lightweight cloud route probe. Only runs pre-flight checks
    for providers that are known to have reachable endpoints.
    
    If probing fails (network down, unreachable, etc.), returns
    the original route without falling back — trusting dsh to
    surface the actual error to the user.
    """
    # Skip pre-flight for opencode-go variants — they need special headers
    if provider.startswith("opencode-go"):
        return provider, model, effort
    
    # Skip for ninfer variants — handled separately
    if provider.startswith("ninfer") or provider == "local-qwen-q6k":
        return provider, model, effort
    
    ok = True
    err_msg = ""

    # Build a simple HTTP GET to validate connectivity
    try:
        if provider == "openrouter":
            url = "https://openrouter.ai/api/v1/models"
        elif provider == "tokenharbor":
            url = "https://tokenharbor.ai/v1/models"
        else:
            # Unknown provider — skip probe, trust routing config
            return provider, model, effort
        
        r = requests.get(url, timeout=5)
        status = r.status_code
        if status == 429:
            ok = False; err_msg = "rate-limited"
        elif status == 402:
            ok = False; err_msg = "insufficient-balance"
        elif status >= 400:
            ok = False; err_msg = f"http-{status}"
    except Exception as e:
        ok = False; err_msg = str(e)[:80]

    if not ok:
        _mark_cloud_exhausted(err_msg)
        print(f"[set_agent_model] Cloud probe failed ({err_msg}). Falling back.",
              file=sys.stderr)
        return _fallback_to_local()

    return provider, model, effort


def _fallback_to_local():
    """Find the first healthy local LLM service and return its route."""
    for name, model, unit, health_url in LOCAL_ALTS:
        try:
            r = requests.get(health_url, timeout=3)
            if r.status_code == 200:
                return name, model, "xhigh"
        except Exception:
            continue

    # No local LLM healthy — warn user
    units = ", ".join(u for _, _, u, _ in LOCAL_ALTS)
    print(f"[set_agent_model] WARNING: no local LLM healthy ({units}). "
          "Agent call will likely fail.", file=sys.stderr)
    return "ninfer", "qwen3.8-27b", "xhigh"


# ── Main entry point ────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <preset> [--show]", file=sys.stderr)
        sys.exit(1)

    preset = sys.argv[1]
    show_only = "--show" in sys.argv

    # Determine which provider to use based on preset type
    if preset in PRO_PRESETS:
        provider, model, effort = _lookup(
            "reasoning", "DSH_REASONING_PROVIDER",
            "DSH_REASONING_MODEL", "DSH_REASONING_EFFORT"
        )
    elif preset in VISION_PRESETS:
        provider, model, effort = _lookup(
            "vision", "DSH_VISION_PROVIDER",
            "DSH_VISION_MODEL", "DSH_VISION_EFFORT"
        )
    else:
        provider, model, effort = _lookup(
            "execution", "DSH_EXECUTION_PROVIDER",
            "DSH_EXECUTION_MODEL", "DSH_EXECUTION_EFFORT"
        )

    # With --show, just print the route — skip all network calls
    if show_only:
        print(f"{preset} -> {provider}/{model} (effort={effort})")
        return 0

    # Check if cloud is exhausted — auto-fallback
    if _is_cloud_exhausted():
        print("[set_agent_model] Cloud is exhausted — using local route.",
              file=sys.stderr)
        provider, model, effort = _fallback_to_local()
    else:
        # Test the cloud route before committing
        try:
            provider, model, effort = _try_cloud_route(provider, model, effort)
        except Exception as e:
            print(f"[set_agent_model] Cloud check failed ({e}), using local.",
                  file=sys.stderr)
            provider, model, effort = _fallback_to_local()

    # Write atomically to settings.yaml
    settings_path = SETTINGS
    # The dsh CLI renames settings.yaml -> settings.yaml.imported on some launches
    # (first-run import behaviour). If it's gone, restore from .imported (or seed
    # a minimal file) so we always have a base to read-modify-write.
    if not os.path.exists(settings_path):
        imported = settings_path + ".imported"
        if os.path.exists(imported):
            shutil.copyfile(imported, settings_path)
            print(f"[set_agent_model] Restored {settings_path} from .imported",
                  file=sys.stderr)
        else:
            with open(settings_path, "w") as fh:
                yaml.dump({"agent-default-model": {}}, fh, default_flow_style=False)
            print(f"[set_agent_model] Seeded empty {settings_path}",
                  file=sys.stderr)
    with open(settings_path) as fh:
        data = yaml.safe_load(fh) or {}

    if "agent-default-model" not in data:
        data["agent-default-model"] = {}
    data["agent-default-model"].update({
        "provider": provider,
        "model": model,
        "reasoningEffort": effort,
    })

    # Write atomically — temp file + rename
    dir_path = os.path.dirname(settings_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_fh:
            yaml.dump(data, tmp_fh, default_flow_style=False)
        os.replace(tmp_path, settings_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if show_only:
        print(f"{preset} -> {provider}/{model} (effort={effort})")
    else:
        print(f"Routed: {preset} -> {provider}/{model} (effort={effort})")

    # Debug: log number of known providers
    nprov = len(data.get("llm-pi-ai", {}).get("providers", {}))
    print(f"[{nprov}] providers available")


if __name__ == "__main__":
    main()
