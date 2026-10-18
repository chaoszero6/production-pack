#!/usr/bin/env python3
"""Per-agent model routing — cloud only, no fallbacks.

Single source of truth for "which model does agent <preset> use". run.sh calls
this before EVERY dsh launch (dsh resolves the model from `agent-default-model`
at launch), so the route can never be inherited from the previous call.

Usage:
    set_agent_model.py <preset>                 write the route, print ROUTE line
    set_agent_model.py <preset> --show          print the route, touch nothing
    set_agent_model.py <preset> --probe         also GET /models before writing
    set_agent_model.py --dry-run-all            print every preset's route
    set_agent_model.py --probe-all              GET /models on every provider in use
    set_agent_model.py --list                   tiers, presets, providers

Exit codes (run.sh turns 2 and 3 into a halt, never a reroute):
    0  route written / printed
    2  routing error     — unknown preset tier, provider or model, bad env
    3  provider error    — missing API key, /models probe failed (status in msg)

Tiers (decision of 2026-10-18 — no local LLM, every agent in the cloud):

    creative   story-creator, story-editor, dialogue-polish, director   anthropic / claude-opus-5-5  effort high
    vision     qa-inspector                                anthropic / claude-haiku-5-5 (decision 2026-10-18;
               alternative verified to read frames: opencode-go-deepseek / qwen3.8-flash)
    execution  everything else                             opencode-go-deepseek / deepseek-v4.1-flash

Env overrides (read once per call, the ONLY way to change a route):
    DSH_CREATIVE_PROVIDER / DSH_CREATIVE_MODEL / DSH_CREATIVE_EFFORT
    DSH_VISION_PROVIDER   / DSH_VISION_MODEL   / DSH_VISION_EFFORT
    DSH_EXECUTION_PROVIDER/ DSH_EXECUTION_MODEL/ DSH_EXECUTION_EFFORT
    DSH_SETTINGS                                 (default /root/.dsh/settings.yaml)
    DSH_ENV_FILE                                 (default /root/.hermes/.env — API keys)
    DSH_FORCE_TIER=execution                     (one call: route this preset on that tier)

Providers must be registered for dsh in /root/.dsh/profiles/headless/cordis.patch.yml
(llm-pi-ai → providers). `anthropic` is a built-in pi-ai provider (api
anthropic-messages, key ANTHROPIC_API_KEY); the OpenCode Go entries are custom
openai-completions providers keyed by OPENCODE_GO_API_KEY.

Opus 5.5 notes: thinking is always on (never send budget_tokens / disabled);
effort is the only depth control and must be explicit — default here is high.
"""
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:  # pragma: no cover
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyyaml"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import yaml

SETTINGS = os.environ.get("DSH_SETTINGS", "/root/.dsh/settings.yaml")
ENV_FILE = os.environ.get("DSH_ENV_FILE", "/root/.hermes/.env")
# dsh imports settings.yaml into its profile patch AT LAUNCH, but the launch that
# does the import still runs with the previously imported value (observed
# 2026-10-18: UNKNOWN_MODEL for the route of the call before). Writing the
# profile's own agent-default-model block too makes the route take effect on the
# very next launch.
PROFILE_PATCH = os.environ.get("DSH_PROFILE_PATCH", "/root/.dsh/profiles/headless/cordis.patch.yml")

EXIT_ROUTING = 2
EXIT_PROVIDER = 3


class RoutingError(Exception):
    """Unknown preset tier / provider / model or a malformed override."""


class ProviderError(Exception):
    """Key missing or the provider's /models probe failed."""


# ── Tier table ───────────────────────────────────────────────────────────────
CREATIVE_PRESETS = {"story-creator", "story-editor", "dialogue-polish", "director"}
VISION_PRESETS = {"qa-inspector"}
# Per-preset model override inside its tier (provider stays the tier's). Empty today:
# OpenCode Go offers no Gemini, so dialogue-polish runs on the creative tier (Opus).
PRESET_MODEL_OVERRIDE = {}

TIER_DEFAULTS = {
    # tier:       (provider,               model,                 effort)
    "creative":  ("anthropic",            "claude-opus-5-5",     "high"),
    "vision":    ("anthropic",            "claude-haiku-5-5",    "high"),
    "execution": ("opencode-go-deepseek", "deepseek-v4.1-flash", "high"),
}

VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# Providers this pack knows how to probe. Anything else is a routing error:
# a typo must never resolve to "whatever comes first".
PROVIDERS = {
    "anthropic": {
        "models_url": "https://api.anthropic.com/v1/models",
        "key_env": "ANTHROPIC_API_KEY",
        "headers": lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
    },
    "opencode-go-deepseek": {
        "models_url": "https://opencode.ai/zen/go/v1/models",
        "key_env": "OPENCODE_GO_API_KEY",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "opencode-go-deepseek-pro": {
        "models_url": "https://opencode.ai/zen/go/v1/models",
        "key_env": "OPENCODE_GO_API_KEY",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "opencode-go": {
        "models_url": "https://opencode.ai/zen/go/v1/models",
        "key_env": "OPENCODE_GO_API_KEY",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
    "openrouter": {
        "models_url": "https://openrouter.ai/api/v1/models",
        "key_env": "OPENROUTER_API_KEY",
        "headers": lambda key: {"Authorization": f"Bearer {key}"},
    },
}

# Models the pack has verified per provider (a route to an unlisted model is a
# routing error — add it here AND to cordis.patch.yml after verifying it).
KNOWN_MODELS = {
    "anthropic": {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"},
    "opencode-go-deepseek": {"deepseek-v4.1-flash", "qwen3.8-flash", "deepseek-v4-flash-vision-exp"},
    "opencode-go-deepseek-pro": {"deepseek-v4-pro"},
    "opencode-go": {"deepseek-v4.1-flash", "qwen3.8-flash", "deepseek-v4-pro"},
    "openrouter": None,  # open catalogue — not validated here
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _env(name, default=None):
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else default


_DOTENV_CACHE = None


def _load_dotenv():
    """Parse ENV_FILE (KEY=VALUE lines) once; never overrides real env vars."""
    global _DOTENV_CACHE
    if _DOTENV_CACHE is not None:
        return _DOTENV_CACHE
    vals = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[7:].strip()
                vals[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    _DOTENV_CACHE = vals
    return vals


def api_key_for(provider):
    spec = PROVIDERS[provider]
    key = _env(spec["key_env"]) or _load_dotenv().get(spec["key_env"], "").strip()
    return key or None


def tier_of(preset):
    """Tier of a preset. DSH_FORCE_TIER=<tier> overrides it for ONE call — run.sh
    uses it to run the Director's mechanical per-scene gate on the execution
    tier (OpenCode Go) instead of spending Opus on a lint fix."""
    forced = _env("DSH_FORCE_TIER")
    if forced:
        if forced not in TIER_DEFAULTS:
            raise RoutingError(f"DSH_FORCE_TIER={forced!r} is not a tier ({', '.join(TIER_DEFAULTS)})")
        return forced
    if preset in CREATIVE_PRESETS:
        return "creative"
    if preset in VISION_PRESETS:
        return "vision"
    return "execution"


def resolve_tier(tier):
    """(provider, model, effort) for a tier after env overrides, validated."""
    if tier not in TIER_DEFAULTS:
        raise RoutingError(f"unknown tier {tier!r}")
    d_provider, d_model, d_effort = TIER_DEFAULTS[tier]
    up = tier.upper()
    provider = _env(f"DSH_{up}_PROVIDER", d_provider)
    model = _env(f"DSH_{up}_MODEL")
    effort = _env(f"DSH_{up}_EFFORT", d_effort)
    if provider not in PROVIDERS:
        raise RoutingError(
            f"provider {provider!r} for tier {tier} is not registered in "
            f"set_agent_model.PROVIDERS ({', '.join(sorted(PROVIDERS))}); "
            f"check DSH_{up}_PROVIDER")
    if model is None:
        if provider != d_provider:
            raise RoutingError(
                f"DSH_{up}_PROVIDER={provider} set without DSH_{up}_MODEL — "
                "a provider change must name the model explicitly")
        model = d_model
    if effort not in VALID_EFFORTS:
        raise RoutingError(f"DSH_{up}_EFFORT={effort!r} not in {sorted(VALID_EFFORTS)}")
    return provider, model, effort


def route_for(preset):
    """Return (provider, model, effort, tier) for a preset. Raises RoutingError."""
    tier = tier_of(preset)
    provider, model, effort = resolve_tier(tier)
    override = PRESET_MODEL_OVERRIDE.get(preset)
    if override and not _env(f"DSH_{tier.upper()}_MODEL"):
        model = override
    known = KNOWN_MODELS.get(provider)
    if known is not None and model not in known:
        raise RoutingError(
            f"model {model!r} is not a verified model for provider {provider!r} "
            f"(known: {', '.join(sorted(known))}); add it to KNOWN_MODELS and "
            "cordis.patch.yml after verifying it")
    return provider, model, effort, tier


def probe_provider(provider, timeout=8):
    """GET the provider's /models with its key. Raises ProviderError; no reroute."""
    spec = PROVIDERS[provider]
    key = api_key_for(provider)
    if not key:
        raise ProviderError(
            f"{provider}: {spec['key_env']} is not set (env or {ENV_FILE})")
    # Cloudflare in front of OpenCode Go rejects Python's default User-Agent (error 1010).
    headers = {"User-Agent": "production-pack/1.0 (+https://github.com/chaoszero6/production-pack)",
               "Accept": "application/json"}
    headers.update(spec["headers"](key))
    req = urllib.request.Request(spec["models_url"], headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise ProviderError(f"{provider}: HTTP {resp.status} from {spec['models_url']}")
            body = resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        raise ProviderError(f"{provider}: HTTP {e.code} from {spec['models_url']} {detail}".strip())
    except urllib.error.URLError as e:
        raise ProviderError(f"{provider}: {spec['models_url']} unreachable ({e.reason})")
    except OSError as e:
        raise ProviderError(f"{provider}: probe failed ({e})")
    return body


def all_presets():
    pack = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(pack, ".dsh", ".agent-presets")
    try:
        return sorted(p for p in os.listdir(d) if os.path.isdir(os.path.join(d, p)))
    except FileNotFoundError:
        return sorted(CREATIVE_PRESETS | VISION_PRESETS | set(PRESET_MODEL_OVERRIDE))


def providers_in_use():
    seen = []
    for preset in all_presets():
        provider = route_for(preset)[0]
        if provider not in seen:
            seen.append(provider)
    return seen


# ── settings.yaml writer ─────────────────────────────────────────────────────
def write_route(provider, model, effort):
    """Atomically update agent-default-model in settings.yaml."""
    path = SETTINGS
    if not os.path.exists(path):
        imported = path + ".imported"
        if os.path.exists(imported):
            shutil.copyfile(imported, path)
            print(f"[set_agent_model] restored {path} from .imported", file=sys.stderr)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump({"agent-default-model": {}}, fh, default_flow_style=False)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("agent-default-model", {})
    data["agent-default-model"].update({
        "provider": provider,
        "model": model,
        "reasoningEffort": effort,
    })
    dir_path = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            yaml.dump(data, tmp_fh, default_flow_style=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


NL = "\n"
_PROFILE_HEAD = ('- id: agent-default-model' + NL +
                 '  name: "@deepseek-ai/dsh-agent-default-model"' + NL +
                 '  config:' + NL)
_PROFILE_BLOCK_RE = re.compile(re.escape(_PROFILE_HEAD) + r"(?:    [a-zA-Z]+: [^\n]*\n)*")


def write_profile_route(provider, model, effort):
    """Atomically set the agent-default-model block of the headless profile patch."""
    path = PROFILE_PATCH
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    block = (_PROFILE_HEAD + f"    provider: {provider}" + NL + f"    model: {model}" + NL
             + f"    reasoningEffort: {effort}" + NL)
    m = _PROFILE_BLOCK_RE.search(text)
    if m:
        new_text = text[:m.start()] + block + text[m.end():]
    else:
        new_text = text.rstrip(NL) + NL + block
    if new_text == text:
        return True
    dir_path = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(new_text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def route_line(preset, provider, model, effort, tier):
    return f"ROUTE preset={preset} provider={provider} model={model} effort={effort} tier={tier}"


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]

    try:
        if cmd == "--list":
            print("tiers:")
            for tier, (p, m, e) in TIER_DEFAULTS.items():
                p2, m2, e2 = resolve_tier(tier)
                flag = "" if (p, m, e) == (p2, m2, e2) else "   (env override)"
                print(f"  {tier:10s} {p2}/{m2} effort={e2}{flag}")
            print(f"creative presets: {', '.join(sorted(CREATIVE_PRESETS))}")
            print(f"vision presets:   {', '.join(sorted(VISION_PRESETS))}")
            print(f"model overrides:  {PRESET_MODEL_OVERRIDE}")
            return 0

        if cmd == "--dry-run-all":
            for preset in all_presets():
                p, m, e, t = route_for(preset)
                print(route_line(preset, p, m, e, t))
            return 0

        if cmd == "--probe-all":
            failed = False
            for provider in providers_in_use():
                try:
                    probe_provider(provider)
                    print(f"PROBE {provider} ok")
                except ProviderError as e:
                    failed = True
                    print(f"PROBE {provider} FAIL {e}")
            return EXIT_PROVIDER if failed else 0

        preset = cmd
        show_only = "--show" in argv
        probe = "--probe" in argv
        provider, model, effort, tier = route_for(preset)
        if show_only:
            print(route_line(preset, provider, model, effort, tier))
            return 0
        if not api_key_for(provider):
            raise ProviderError(
                f"{provider}: {PROVIDERS[provider]['key_env']} is not set (env or {ENV_FILE})")
        if probe:
            probe_provider(provider)
        write_route(provider, model, effort)
        write_profile_route(provider, model, effort)
        print(f"Routed: {preset} -> {provider}/{model} (effort={effort}, tier={tier})")
        print(route_line(preset, provider, model, effort, tier))
        return 0

    except RoutingError as e:
        print(f"ROUTING ERROR: {e}", file=sys.stderr)
        return EXIT_ROUTING
    except ProviderError as e:
        print(f"PROVIDER ERROR: {e}", file=sys.stderr)
        return EXIT_PROVIDER


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
