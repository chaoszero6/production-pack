#!/usr/bin/env python3
"""music_render.py — validate the Music Composer's cue sheet and render it with ACE-Step 1.5.

Why a cue sheet: ComfyUI and the TTS cluster are the exclusive GPU owners (no local LLM) (systemd Conflicts=),
so the composer cannot call ACE-Step during its turn. It PLANS music/music_cues.json
(schema + rules: .dsh/skills/music-cues.md); this script resolves clip anchors against
final/timeline.json, enforces the rules, splits long cues, and renders every cue and bed
through the core ACE-Step 1.5 nodes (ComfyUI >= 0.39) with the GPU handed to ComfyUI.

  --validate   resolve + check only, write music/music_cues.resolved.json, exit 2 on problems
  render       validate first (same rules), then render what is missing or changed

Graph per cue (official ACE-Step 1.5 turbo template):
  CheckpointLoaderSimple(ace_step_1.5_turbo_aio) -> TextEncodeAceStepAudio1.5(tags, "[Instrumental]",
  bpm, duration, key, time signature) -> ConditioningZeroOut (negative) ->
  ModelSamplingAuraFlow(shift 3) -> EmptyAceStep1.5LatentAudio(seconds) ->
  KSampler(8 steps, cfg 1.0, euler, simple) -> VAEDecodeAudio -> SaveAudio (FLAC) -> ffmpeg 48 kHz.

music/music_cues.resolved.json is what assemble.py must read (anchors resolved, long cues split
into <id>a/<id>b pieces, plus assemble's key names film_start_s/film_end_s/crossfade_s/level_db).
Outputs: music/cues/<id>.wav + <id>.json sidecar {prompt, seed, seconds, lufs, engine, ckpt, ts,
cue_hash}; beds under music/beds/; music/render_log.json {"cues": {id: {status, ...}}, "ok", "failed"}.
Idempotent: a cue whose sidecar cue_hash matches is skipped (--force re-renders).

Exit 0 all rendered · 1 partial · 2 bad cue sheet · 3 ComfyUI / nodes / checkpoint missing.

  python3 music_render.py --run-dir D [--cues music/music_cues.json] [--validate] [--force]
                          [--only M01,A01] [--max-cue-s 180] [--keep-gpu] [--no-gpu-switch]
                          [--comfy http://127.0.0.1:8188]

The GPU is NOT handed back afterwards (the LLM is cloud now); ComfyUI stays owner.
--keep-gpu is accepted for compatibility. urllib only — nothing to pip install on the host.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time

PIPE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPE)
from sfx_render import (  # noqa: E402  (shared ComfyUI http helpers + MMAudio graph)
    SFX_NEG, VOCAL_RE, audio_outputs, comfy_alive, download, ff, free_comfy, gpu, http_json,
    mmaudio_graph, node_info, queue, upload_file, wait,
)

COMFY_DEFAULT = "http://127.0.0.1:8188"
ENGINE = "acestep-1.5-turbo"
CKPT = "ace_step_1.5_turbo_aio.safetensors"
ENCODER_NODE = "TextEncodeAceStepAudio1.5"
LATENT_NODE = "EmptyAceStep1.5LatentAudio"
ACE_NODES = ("CheckpointLoaderSimple", ENCODER_NODE, "ConditioningZeroOut", "ModelSamplingAuraFlow",
             LATENT_NODE, "KSampler", "VAEDecodeAudio", "SaveAudio")
# official turbo recipe
SHIFT, STEPS, CFG, SAMPLER, SCHEDULER = 3.0, 8, 1.0, "euler", "simple"
INSTRUMENTAL_LYRICS = "[Instrumental]"

MIN_CUE_S, MAX_CUE_S_DEFAULT = 10.0, 180.0
MAX_GAP_S = 0.5
OVERLAP_TOL_S = 0.05
SPLIT_XFADE_S = 2.0
TEMPO_RANGE = (50, 160)
TRANSITIONS = {"fade", "crossfade", "hard", "none"}
BED_ENGINES = {"acestep", "mmaudio"}
# a negated word in a positive prompt still pulls toward what it names, so these never appear in tags
TAG_BAN_RE = re.compile(r"\b(vocals?|vocalist|lyrics?|sing(ing|er|ers|s)?|sung|choir|chorus vocals|"
                        r"hum(med|ming)?|a ?cappella|rap(ping)?|spoken word|voice|voices)\b", re.I)


def log(msg: str) -> None:
    print(f"[music-render {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
# pure functions — validation / resolution (no I/O, unit-tested)
# ═══════════════════════════════════════════════════════════════════════════════
def cue_hash(cue: dict) -> str:
    """sha1 of the cue as the composer wrote it (resolved times included, sidecar-only keys excluded)."""
    d = {k: v for k, v in cue.items() if k not in ("render",)}
    return hashlib.sha1(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _num(x) -> float | None:
    try:
        if isinstance(x, bool):
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def timeline_index(timeline: dict) -> tuple[dict, dict, float]:
    """-> ({shot_id: segment}, {scene_id: scene}, film_duration_s)."""
    segs = {s["shot_id"]: s for s in (timeline.get("segments") or []) if isinstance(s, dict) and s.get("shot_id")}
    scenes = {s["scene_id"]: s for s in (timeline.get("scenes") or []) if isinstance(s, dict) and s.get("scene_id")}
    if not scenes and segs:  # derive scene spans from the segments when assemble did not list them
        for s in segs.values():
            sc = scenes.setdefault(s.get("scene_id") or "?", {"scene_id": s.get("scene_id"), "film_start_s": s["film_start_s"],
                                                               "film_end_s": s["film_end_s"], "shots": []})
            sc["film_start_s"] = min(sc["film_start_s"], s["film_start_s"])
            sc["film_end_s"] = max(sc["film_end_s"], s["film_end_s"])
            sc["shots"].append(s["shot_id"])
    dur = _num(timeline.get("film_duration_s"))
    if dur is None:
        dur = max((float(s.get("film_end_s") or 0) for s in segs.values()), default=0.0)
    return segs, scenes, dur


def resolve_times(item: dict, segs: dict, problems: list[str], label: str) -> tuple[float | None, float | None]:
    """Clip anchors win over start_s/end_s; both are returned as film seconds."""
    anchor = item.get("anchor")
    if isinstance(anchor, dict) and (anchor.get("start_clip") or anchor.get("end_clip")):
        a, b = anchor.get("start_clip"), anchor.get("end_clip") or anchor.get("start_clip")
        if a not in segs:
            problems.append(f"{label}: anchor.start_clip {a!r} is not in timeline.json")
            return None, None
        if b not in segs:
            problems.append(f"{label}: anchor.end_clip {b!r} is not in timeline.json")
            return None, None
        start, end = float(segs[a]["film_start_s"]), float(segs[b]["film_end_s"])
        if end <= start:
            problems.append(f"{label}: anchor {a}..{b} runs backwards ({start:.2f} -> {end:.2f})")
            return None, None
        return start, end
    start, end = _num(item.get("start_s")), _num(item.get("end_s"))
    if start is None or end is None:
        problems.append(f"{label}: needs an anchor {{start_clip, end_clip}} or numeric start_s/end_s")
        return None, None
    if end <= start:
        problems.append(f"{label}: end_s {end:.2f} must be after start_s {start:.2f}")
        return None, None
    return start, end


def _transition(item: dict, key: str, label: str, problems: list[str]) -> dict:
    t = item.get(key)
    if t is None:
        return {"type": "fade", "s": 1.0}
    if not isinstance(t, dict):
        problems.append(f"{label}: {key} must be an object {{type, s}}")
        return {"type": "fade", "s": 1.0}
    typ = str(t.get("type") or "fade").lower()
    if typ not in TRANSITIONS:
        problems.append(f"{label}: {key}.type {typ!r} not in {sorted(TRANSITIONS)}")
    s = _num(t.get("s"))
    if s is None or s < 0:
        s = 0.0 if typ in ("hard", "none") else 1.0
    return {"type": typ, "s": float(s)}


def text_mentions_vocals(*texts) -> str | None:
    for t in texts:
        if isinstance(t, list):
            t = " ".join(str(x) for x in t)
        if isinstance(t, str):
            m = TAG_BAN_RE.search(t) or VOCAL_RE.search(t)
            if m:
                return m.group(0)
    return None


def split_cue(cue: dict, max_cue_s: float) -> list[dict]:
    """A cue longer than max_cue_s becomes chained pieces (M03a, M03b, ...) joined by a 2 s
    crossfade, each with seed + 1 so the pieces are different takes of the same material."""
    length = cue["end_s"] - cue["start_s"]
    if length <= max_cue_s:
        return [cue]
    n = int(math.ceil(length / max_cue_s))
    piece = length / n
    pieces = []
    for i in range(n):
        p = copy.deepcopy(cue)
        p["id"] = f"{cue['id']}{chr(ord('a') + i)}"
        p["parent_id"] = cue["id"]
        p["start_s"] = cue["start_s"] + i * piece - (SPLIT_XFADE_S if i > 0 else 0.0)
        p["end_s"] = cue["start_s"] + (i + 1) * piece
        p["seed"] = int(cue.get("seed", 0)) + i
        if i > 0:
            p["transition_in"] = {"type": "crossfade", "s": SPLIT_XFADE_S}
        if i < n - 1:
            p["transition_out"] = {"type": "crossfade", "s": SPLIT_XFADE_S}
        p.pop("anchor", None)
        pieces.append(p)
    return pieces


def _check_tiling(scene: dict, cues: list[dict], problems: list[str]) -> None:
    """Cues covering a scene must tile it: gaps <= 0.5 s, overlaps only by the crossfade seconds."""
    sid = scene["scene_id"]
    s0, s1 = float(scene["film_start_s"]), float(scene["film_end_s"])
    inside = sorted((c for c in cues if c["end_s"] > s0 + 1e-6 and c["start_s"] < s1 - 1e-6), key=lambda c: c["start_s"])
    if not inside:
        return
    if inside[0]["start_s"] - s0 > MAX_GAP_S:
        problems.append(f"scene {sid}: first cue {inside[0]['id']} starts {inside[0]['start_s'] - s0:.2f}s after the scene "
                        f"start (max {MAX_GAP_S}s)")
    for a, b in zip(inside, inside[1:]):
        gap = b["start_s"] - a["end_s"]
        if gap > MAX_GAP_S:
            problems.append(f"scene {sid}: gap of {gap:.2f}s between {a['id']} and {b['id']} (max {MAX_GAP_S}s)")
        elif gap < 0:
            allowed = max(a["transition_out"]["s"] if a["transition_out"]["type"] == "crossfade" else 0.0,
                          b["transition_in"]["s"] if b["transition_in"]["type"] == "crossfade" else 0.0)
            if -gap > allowed + OVERLAP_TOL_S:
                problems.append(f"scene {sid}: {a['id']} and {b['id']} overlap {-gap:.2f}s but the crossfade is "
                                f"{allowed:.2f}s (overlap only by crossfade seconds)")
    last_end = max(c["end_s"] for c in inside)
    if s1 - last_end > MAX_GAP_S:
        problems.append(f"scene {sid}: last cue ends {s1 - last_end:.2f}s before the scene end (max {MAX_GAP_S}s)")


def resolve_cues(doc: dict, timeline: dict, max_cue_s: float = MAX_CUE_S_DEFAULT) -> tuple[dict, list[str]]:
    """Resolve anchors, enforce the music-cues rules, split long cues.
    Returns (resolved_doc, problems). problems == [] means the sheet is renderable."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return {}, ["music_cues.json must be a JSON object"]
    segs, scenes, film_dur = timeline_index(timeline)
    if not segs:
        return {}, ["timeline.json has no segments — run assemble.py first"]
    out = copy.deepcopy(doc)
    out["version"] = int(doc.get("version") or 1)
    out["film_duration_s"] = round(film_dur, 3)
    g = out.setdefault("global", {})
    if not isinstance(g, dict):
        problems.append("global must be an object")
        g = out["global"] = {}
    if g.get("vocals") not in (False, None):
        problems.append("global.vocals must be false")
    g["vocals"] = False
    g.setdefault("music_level_db", -6)
    g.setdefault("bed_level_db", -18)
    if (w := text_mentions_vocals(g.get("style_tags"), g.get("notes"))):
        problems.append(f"global.style_tags/notes mention {w!r} — the score is instrumental")

    # ── cues
    cues_in = doc.get("cues")
    if not isinstance(cues_in, list):
        problems.append("cues must be a list")
        cues_in = []
    resolved: list[dict] = []
    seen: set[str] = set()
    for i, cue in enumerate(cues_in):
        if not isinstance(cue, dict):
            problems.append(f"cues[{i}]: not an object")
            continue
        c = copy.deepcopy(cue)
        cid = str(c.get("id") or f"M{i + 1:02d}")
        c["id"] = cid
        label = f"cue {cid}"
        if cid in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(cid)
        if c.get("scene_id") not in scenes:
            problems.append(f"{label}: scene_id {c.get('scene_id')!r} is not in timeline.json")
        start, end = resolve_times(c, segs, problems, label)
        if start is None:
            continue
        c["start_s"], c["end_s"] = round(start, 3), round(end, 3)
        if end > film_dur + MAX_GAP_S:
            problems.append(f"{label}: ends at {end:.2f}s, past the film ({film_dur:.2f}s)")
        if c.get("vocals") not in (False, None):
            problems.append(f"{label}: vocals must be false")
        c["vocals"] = False
        tempo = _num(c.get("tempo_bpm"))
        if tempo is None or not (TEMPO_RANGE[0] <= tempo <= TEMPO_RANGE[1]):
            problems.append(f"{label}: tempo_bpm {c.get('tempo_bpm')!r} must be {TEMPO_RANGE[0]}-{TEMPO_RANGE[1]}")
        else:
            c["tempo_bpm"] = int(round(tempo))
        inten = _num(c.get("intensity"))
        if inten is None or not (0.0 <= inten <= 1.0):
            problems.append(f"{label}: intensity {c.get('intensity')!r} must be 0-1")
        else:
            c["intensity"] = inten
        if not isinstance(c.get("genre_tags"), list) or not c["genre_tags"]:
            problems.append(f"{label}: genre_tags must be a non-empty list")
        if not c.get("mood"):
            problems.append(f"{label}: mood is required")
        if (w := text_mentions_vocals(c.get("mood"), c.get("instrumentation"), c.get("genre_tags"), c.get("notes"))):
            problems.append(f"{label}: prompt text mentions {w!r} — never ask for vocals/lyrics/singing")
        c["transition_in"] = _transition(c, "transition_in", label, problems)
        c["transition_out"] = _transition(c, "transition_out", label, problems)
        c["tail_s"] = max(0.0, _num(c.get("tail_s")) or 0.0)
        c["seed"] = int(_num(c.get("seed")) or 0) or (int(hashlib.sha1(cid.encode()).hexdigest()[:7], 16))
        c.setdefault("key", g.get("key") or "")
        c.setdefault("time_signature", "4")
        c.setdefault("loopable", False)
        lvl = _num(c.get("level_db"))
        if c.get("level_db") is not None and lvl is None:
            problems.append(f"{label}: level_db must be a number (dB trim relative to global.music_level_db)")
        c["level_db"] = lvl or 0.0
        length = end - start
        if length < MIN_CUE_S:
            problems.append(f"{label}: {length:.1f}s is shorter than the {MIN_CUE_S:.0f}s minimum render — extend the "
                            f"anchor to a neighbouring clip or cover this scene from the previous cue")
        resolved.extend(split_cue(c, max_cue_s))

    # ── beds
    beds_in = doc.get("beds") or []
    if not isinstance(beds_in, list):
        problems.append("beds must be a list")
        beds_in = []
    beds: list[dict] = []
    for i, bed in enumerate(beds_in):
        if not isinstance(bed, dict):
            problems.append(f"beds[{i}]: not an object")
            continue
        b = copy.deepcopy(bed)
        bid = str(b.get("id") or f"A{i + 1:02d}")
        b["id"] = bid
        label = f"bed {bid}"
        if bid in seen:
            problems.append(f"{label}: duplicate id")
        seen.add(bid)
        if b.get("scene_id") not in scenes:
            problems.append(f"{label}: scene_id {b.get('scene_id')!r} is not in timeline.json")
        start, end = resolve_times(b, segs, problems, label)
        if start is None:
            continue
        b["start_s"], b["end_s"] = round(start, 3), round(end, 3)
        if not b.get("prompt"):
            problems.append(f"{label}: prompt is required")
        if (w := text_mentions_vocals(b.get("prompt"))):
            problems.append(f"{label}: prompt mentions {w!r} — beds never ask for voices")
        eng = str(b.get("engine") or "acestep").lower()
        if eng not in BED_ENGINES:
            problems.append(f"{label}: engine {eng!r} not in {sorted(BED_ENGINES)}")
        b["engine"] = eng
        lvl = _num(b.get("level_db"))
        if b.get("level_db") is not None and lvl is None:
            problems.append(f"{label}: level_db must be a number (dB trim relative to global.bed_level_db)")
        b["level_db"] = lvl or 0.0
        b["seed"] = int(_num(b.get("seed")) or 0) or int(hashlib.sha1(bid.encode()).hexdigest()[:7], 16)
        b["transition_in"] = _transition(b, "transition_in", label, problems)
        b["transition_out"] = _transition(b, "transition_out", label, problems)
        b["tail_s"] = max(0.0, _num(b.get("tail_s")) or 0.0)
        if end - start < MIN_CUE_S:
            problems.append(f"{label}: {end - start:.1f}s is shorter than the {MIN_CUE_S:.0f}s minimum render")
        beds.extend(split_cue(b, max_cue_s))

    # ── silence + coverage
    silence_in = doc.get("silence") or []
    if not isinstance(silence_in, list):
        problems.append("silence must be a list")
        silence_in = []
    silent: set[str] = set()
    for i, s in enumerate(silence_in):
        if not isinstance(s, dict) or not s.get("scene_id"):
            problems.append(f"silence[{i}]: needs {{scene_id, reason}}")
            continue
        if s["scene_id"] not in scenes:
            problems.append(f"silence[{i}]: scene_id {s['scene_id']!r} is not in timeline.json")
        silent.add(s["scene_id"])
    for scene_id, sc in scenes.items():
        s0, s1 = float(sc["film_start_s"]), float(sc["film_end_s"])
        covered = any(c["end_s"] > s0 + 1e-6 and c["start_s"] < s1 - 1e-6 for c in resolved)
        if scene_id in silent and covered:
            problems.append(f"scene {scene_id}: listed in silence but a cue covers it — remove one")
        if not covered and scene_id not in silent:
            problems.append(f"scene {scene_id}: has neither a cue nor a silence entry")
        if covered:
            _check_tiling(sc, resolved, problems)

    # assemble.py reads the resolved sheet with its own key names: film_start_s / film_end_s,
    # crossfade_s and per-item level_db trims, music_level_db / ref_lufs at the top level.
    for item in resolved + beds:
        item["film_start_s"], item["film_end_s"] = item["start_s"], item["end_s"]
        xf = item["transition_out"]["s"] if item["transition_out"]["type"] == "crossfade" else 0.0
        item["crossfade_s"] = xf
    out["music_level_db"] = _num(g.get("music_level_db")) if _num(g.get("music_level_db")) is not None else -6.0
    out["bed_level_db"] = _num(g.get("bed_level_db")) if _num(g.get("bed_level_db")) is not None else -18.0
    out["ref_lufs"] = _num(g.get("ref_lufs")) if _num(g.get("ref_lufs")) is not None else -18.0
    out["cues"] = resolved
    out["beds"] = beds
    out["silence"] = [s for s in silence_in if isinstance(s, dict)]
    out["resolved_ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out, problems


# ═══════════════════════════════════════════════════════════════════════════════
# pure functions — prompt + graph (unit-tested)
# ═══════════════════════════════════════════════════════════════════════════════
def intensity_word(x: float) -> str:
    x = max(0.0, min(1.0, float(x)))
    if x < 0.2:
        return "sparse and quiet, minimal, delicate"
    if x < 0.4:
        return "soft and gentle, understated"
    if x < 0.6:
        return "moderate, flowing, steady"
    if x < 0.8:
        return "building, driving, energetic"
    return "intense, climactic and powerful, full orchestra"


def build_tags(cue: dict, global_cfg: dict | None = None) -> str:
    """genre_tags + instrumentation + mood + instrumental + bpm + intensity word.
    The word 'vocals' never appears — not even negated — see the module docstring."""
    g = global_cfg or {}
    parts: list[str] = []

    def add(x):
        for p in (x if isinstance(x, list) else [x]):
            if isinstance(p, str):
                for piece in p.split(","):
                    piece = piece.strip()
                    if piece and piece.lower() not in [q.lower() for q in parts]:
                        parts.append(piece)

    add(g.get("style_tags"))
    add(cue.get("genre_tags"))
    add(cue.get("instrumentation"))
    add(cue.get("mood"))
    add("instrumental")
    if cue.get("tempo_bpm"):
        add(f"{int(cue['tempo_bpm'])} bpm")
    if cue.get("intensity") is not None:
        add(intensity_word(cue["intensity"]))
    if cue.get("key"):
        add(str(cue["key"]))
    tags = ", ".join(p for p in parts if not TAG_BAN_RE.search(p))
    return tags


def build_bed_tags(bed: dict) -> str:
    """Ambience bed through the ACE path: the composer's prompt + ambient framing, voice words stripped."""
    parts: list[str] = []
    for piece in str(bed.get("prompt") or "").split(","):
        piece = piece.strip()
        if piece and not VOCAL_RE.search(piece) and not TAG_BAN_RE.search(piece):
            parts.append(piece)
    for extra in ("ambient", "soundscape", "field recording", "atmosphere", "instrumental", "drone"):
        if extra.lower() not in [p.lower() for p in parts]:
            parts.append(extra)
    return ", ".join(parts)


def render_seconds(cue: dict, max_cue_s: float = MAX_CUE_S_DEFAULT) -> float:
    """end - start + tail + outgoing crossfade, clamped to [10, max]."""
    length = float(cue["end_s"]) - float(cue["start_s"])
    xf = cue.get("transition_out") or {}
    extra = float(cue.get("tail_s") or 0.0) + (float(xf.get("s") or 0.0) if xf.get("type") == "crossfade" else 0.0)
    return round(max(MIN_CUE_S, min(max_cue_s, length + extra)), 2)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9#]", "", str(s).lower().replace("♭", "b").replace("♯", "#"))


def pick_combo(options: list, wanted, fallback=None) -> str:
    """Closest match in a node COMBO option list (exact > normalised > prefix > substring)."""
    opts = [o for o in (options or []) if isinstance(o, str)]
    if not opts:
        return str(wanted if wanted is not None else fallback or "")
    w = str(wanted if wanted is not None else "")
    if w in opts:
        return w
    nw = _norm(w)
    if nw:
        aliases = {nw}
        if nw.endswith("major"):
            aliases.add(nw[:-5] + "maj"); aliases.add(nw[:-5])
        if nw.endswith("minor"):
            aliases.add(nw[:-5] + "min"); aliases.add(nw[:-5] + "m")
        if nw.endswith("maj"):
            aliases.add(nw[:-3] + "major")
        if nw.endswith("min"):
            aliases.add(nw[:-3] + "minor")
        for o in opts:
            if _norm(o) in aliases:
                return o
        for o in opts:
            no = _norm(o)
            if no and (no.startswith(nw) or nw.startswith(no)):
                return o
        for o in opts:
            if nw in _norm(o):
                return o
    if fallback is not None and fallback in opts:
        return fallback
    if "" in opts:
        return ""
    return opts[0]


def acestep_graph(tags: str, seconds: float, seed: int, prefix: str, bpm: int = 120, keyscale: str = "",
                  timesignature: str = "4", language: str = "en", lyrics: str = INSTRUMENTAL_LYRICS,
                  ckpt: str = CKPT) -> dict:
    """The ACE-Step 1.5 turbo graph (node ids fixed so tests and logs can address them)."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": ENCODER_NODE, "inputs": {
            "clip": ["1", 1], "tags": tags, "lyrics": lyrics, "seed": int(seed), "bpm": int(bpm),
            "duration": float(seconds), "timesignature": timesignature, "language": language,
            "keyscale": keyscale, "generate_audio_codes": True, "cfg_scale": 2.0, "temperature": 0.85,
            "top_p": 0.9, "top_k": 0, "min_p": 0.0}},
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": SHIFT}},
        "5": {"class_type": LATENT_NODE, "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["4", 0], "seed": int(seed), "steps": STEPS, "cfg": CFG, "sampler_name": SAMPLER,
            "scheduler": SCHEDULER, "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["5", 0],
            "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudio", "inputs": {"audio": ["7", 0], "filename_prefix": prefix}},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# I/O helpers
# ═══════════════════════════════════════════════════════════════════════════════
def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def measure_lufs(path: str) -> float | None:
    """Integrated loudness via ffmpeg's ebur128 (the 'I:' line of the summary)."""
    try:
        r = subprocess.run(["ffmpeg", "-nostats", "-hide_banner", "-i", path, "-af", "ebur128=framelog=quiet",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=600)
        m = re.findall(r"I:\s+(-?[\d.]+)\s+LUFS", r.stderr)
        return float(m[-1]) if m else None
    except Exception:
        return None


def ffprobe_dur(path: str) -> float | None:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:
        return None


def combos(info: dict) -> dict:
    """COMBO option lists of a node's required inputs: {input_name: [options]}."""
    out = {}
    for sect in ("required", "optional"):
        for name, spec in ((info.get("input") or {}).get(sect) or {}).items():
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                out[name] = spec[0]
    return out


def sidecar_matches(wav: str, side: str, h: str) -> bool:
    if not (os.path.isfile(wav) and os.path.isfile(side)):
        return False
    try:
        return load_json(side).get("cue_hash") == h
    except Exception:
        return False


def render_one(comfy: str, item: dict, kind: str, out_dir: str, opts: dict, max_cue_s: float,
               run_dir: str) -> dict:
    """Render one cue/bed to <out_dir>/<id>.wav + .json. Returns the sidecar dict (with status)."""
    cid = item["id"]
    wav = os.path.join(out_dir, f"{cid}.wav")
    side = os.path.join(out_dir, f"{cid}.json")
    seconds = render_seconds(item, max_cue_s)
    seed = int(item["seed"])
    t0 = time.time()
    engine, ckpt = ENGINE, CKPT
    if kind == "bed":
        video = item.get("video")
        if item.get("engine") == "mmaudio" and video:
            vpath = video if os.path.isabs(video) else os.path.join(run_dir, video)
            if not os.path.isfile(vpath):
                raise RuntimeError(f"bed video not found: {vpath}")
            engine, ckpt = "mmaudio-v2v", "mmaudio_large_44k_v2_fp16"
            prompt = ", ".join(p for p in str(item["prompt"]).split(",") if p.strip() and not VOCAL_RE.search(p))
            name = upload_file(comfy, vpath, f"bed_{cid}.mp4")
            wf = mmaudio_graph(name, prompt, min(seconds, 30.0), seed, f"music/beds/{cid}", opts.get("vocoder"),
                               SFX_NEG)
        else:
            if item.get("engine") == "mmaudio":
                log(f"  {cid}: engine mmaudio but the bed has no video — rendering through the ACE path")
            prompt = build_bed_tags(item)
            wf = acestep_graph(prompt, seconds, seed, f"music/beds/{cid}", bpm=60,
                               keyscale=pick_combo(opts["keyscale"], "", ""),
                               timesignature=pick_combo(opts["timesignature"], "4"),
                               language=pick_combo(opts["language"], "en", "en"))
    else:
        prompt = build_tags(item, opts.get("global"))
        key = pick_combo(opts["keyscale"], item.get("key") or "", "")
        if item.get("key") and _norm(key) != _norm(item["key"]) and not (key == "" and not opts["keyscale"]):
            log(f"  {cid}: key {item['key']!r} not an option of {ENCODER_NODE}; using {key!r}")
        wf = acestep_graph(prompt, seconds, seed, f"music/cues/{cid}", bpm=int(item.get("tempo_bpm") or 120),
                           keyscale=key, timesignature=pick_combo(opts["timesignature"], str(item.get("time_signature") or "4")),
                           language=pick_combo(opts["language"], "en", "en"))
    log(f"  {cid}: {seconds:.0f}s seed {seed} — {prompt[:100]}")
    entry = wait(comfy, queue(comfy, wf, f"music-{cid}"), timeout=1800)
    out_item = next(audio_outputs(entry), None)
    if not out_item:
        raise RuntimeError("no audio in ComfyUI outputs")
    raw = os.path.join(out_dir, f"{cid}_raw" + os.path.splitext(out_item["filename"])[1])
    download(comfy, out_item, raw)
    ff("-i", raw, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", wav)
    os.remove(raw)
    meta = {"id": cid, "kind": kind, "status": "ok", "prompt": prompt, "seed": seed, "seconds": seconds,
            "measured_s": ffprobe_dur(wav), "lufs": measure_lufs(wav), "engine": engine, "ckpt": ckpt,
            "start_s": item["start_s"], "end_s": item["end_s"], "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cue_hash": cue_hash(item), "render_s": round(time.time() - t0, 1), "out": wav}
    json.dump(meta, open(side, "w", encoding="utf-8"), indent=1)
    return meta


# ═══════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--cues", default="music/music_cues.json")
    ap.add_argument("--timeline", default="final/timeline.json")
    ap.add_argument("--validate", action="store_true", help="resolve + check only; no render")
    ap.add_argument("--force", action="store_true", help="re-render even when the sidecar hash matches")
    ap.add_argument("--only", default="", help="comma-separated cue/bed ids (M01 also matches its pieces M01a, M01b)")
    ap.add_argument("--max-cue-s", type=float, default=MAX_CUE_S_DEFAULT)
    ap.add_argument("--keep-gpu", action="store_true", help="accepted for compatibility; ComfyUI stays owner anyway")
    ap.add_argument("--no-gpu-switch", action="store_true", help="assume ComfyUI already owns the GPU")
    ap.add_argument("--comfy", default=COMFY_DEFAULT)
    args = ap.parse_args()
    comfy = args.comfy.rstrip("/")
    run_dir = os.path.abspath(args.run_dir)
    cues_path = args.cues if os.path.isabs(args.cues) else os.path.join(run_dir, args.cues)
    tl_path = args.timeline if os.path.isabs(args.timeline) else os.path.join(run_dir, args.timeline)
    music_dir = os.path.join(run_dir, "music")
    resolved_path = os.path.join(music_dir, "music_cues.resolved.json")
    log_path = os.path.join(music_dir, "render_log.json")

    try:
        doc = load_json(cues_path)
    except Exception as exc:
        log(f"cannot read {cues_path}: {exc} — an agent that crashed leaves prose here")
        return 2
    try:
        timeline = load_json(tl_path)
    except Exception as exc:
        log(f"cannot read {tl_path}: {exc} — assemble.py writes it (clip anchors need it)")
        return 2

    resolved, problems = resolve_cues(doc, timeline, args.max_cue_s)
    os.makedirs(music_dir, exist_ok=True)
    if problems:
        log(f"{len(problems)} problem(s) in {os.path.relpath(cues_path, run_dir)}:")
        for p in problems:
            print(f"  - {p}", flush=True)
        json.dump({"problems": problems, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                  open(os.path.join(music_dir, "music_cues.problems.json"), "w", encoding="utf-8"), indent=1)
        return 2
    json.dump(resolved, open(resolved_path, "w", encoding="utf-8"), indent=1)
    n_c, n_b = len(resolved["cues"]), len(resolved["beds"])
    log(f"cue sheet OK: {n_c} cue(s), {n_b} bed(s), {len(resolved['silence'])} silent scene(s), "
        f"film {resolved['film_duration_s']:.1f}s -> {os.path.relpath(resolved_path, run_dir)}")
    if args.validate:
        return 0

    # ── what needs rendering
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    work: list[tuple[str, dict, str]] = []
    results: dict[str, dict] = {}
    for kind, items, sub in (("cue", resolved["cues"], "cues"), ("bed", resolved["beds"], "beds")):
        out_dir = os.path.join(music_dir, sub)
        for it in items:
            if only and it["id"] not in only and it.get("parent_id") not in only:
                continue
            h = cue_hash(it)
            if not args.force and sidecar_matches(os.path.join(out_dir, f"{it['id']}.wav"),
                                                  os.path.join(out_dir, f"{it['id']}.json"), h):
                results[it["id"]] = {"status": "skipped", "reason": "rendered, cue unchanged",
                                     "out": os.path.join(out_dir, f"{it['id']}.wav")}
                continue
            work.append((kind, it, out_dir))
    if not work:
        log("nothing to render (every cue is up to date)")
        json.dump({"cues": results, "ok": 0, "failed": [], "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                  open(log_path, "w", encoding="utf-8"), indent=1)
        return 0
    log(f"{len(work)} item(s) to render: {', '.join(i['id'] for _, i, _ in work)}")

    # ── GPU + ComfyUI preflight
    if not args.no_gpu_switch:
        log("handing the GPU to ComfyUI")
        if not gpu("comfyui"):
            log("ComfyUI did not come up healthy")
            return 3
    if not comfy_alive(comfy):
        log(f"ComfyUI unreachable at {comfy}")
        return 3
    free_comfy(comfy)
    enc = node_info(comfy, ENCODER_NODE)
    if not enc:
        log(f"ComfyUI has no {ENCODER_NODE} node — ACE-Step 1.5 needs ComfyUI >= 0.39 (core nodes); update ComfyUI")
        return 3
    missing = [n for n in ACE_NODES if n != ENCODER_NODE and node_info(comfy, n) is None]
    if missing:
        log(f"ComfyUI lacks node(s): {', '.join(missing)}")
        return 3
    enc_opts = combos(enc)
    for k in ("timesignature", "keyscale", "language"):
        if k not in enc_opts:
            log(f"{ENCODER_NODE} has no COMBO input {k!r} (node signature changed?) — refusing to guess")
            return 3
    ck = combos(node_info(comfy, "CheckpointLoaderSimple") or {}).get("ckpt_name", [])
    if CKPT not in ck:
        log(f"checkpoint {CKPT} is not in ComfyUI's checkpoints list — run ./setup.sh --models (or wait for the "
            f"download to finish) and refresh ComfyUI")
        return 3
    opts = dict(enc_opts)
    opts["global"] = resolved.get("global") or {}
    if any(kind == "bed" and it.get("engine") == "mmaudio" and it.get("video") for kind, it, _ in work):
        fu = node_info(comfy, "MMAudioFeatureUtilsLoader")
        if not fu:
            log("a bed asks for engine mmaudio with a video but ComfyUI lacks the MMAudio nodes")
            return 3
        vopts = ((fu.get("input") or {}).get("optional") or {}).get("bigvgan_vocoder_model", [[]])[0]
        opts["vocoder"] = next((o for o in vopts if isinstance(o, str) and o.endswith("bigvgan_generator.pt")), None)

    ok, failed = 0, []
    try:
        for kind, it, out_dir in work:
            os.makedirs(out_dir, exist_ok=True)
            try:
                meta = render_one(comfy, it, kind, out_dir, opts, args.max_cue_s, run_dir)
                results[it["id"]] = meta
                ok += 1
                lufs = meta.get("lufs")
                log(f"  {it['id']}: {os.path.relpath(meta['out'], run_dir)} {meta.get('measured_s') or 0:.1f}s "
                    f"{'' if lufs is None else f'{lufs:.1f} LUFS '}in {meta['render_s']:.0f}s")
            except Exception as exc:
                results[it["id"]] = {"status": "error", "kind": kind, "error": str(exc)[:500]}
                failed.append(it["id"])
                log(f"  {it['id']}: FAILED {exc}")
    finally:
        json.dump({"cues": results, "ok": ok, "failed": failed, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
                  open(log_path, "w", encoding="utf-8"), indent=1)
    log(f"{ok}/{len(work)} rendered -> {os.path.relpath(log_path, run_dir)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
