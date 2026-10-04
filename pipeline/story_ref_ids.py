#!/usr/bin/env python3
"""Resolve canonical reference ids for story.json characters and locations.

story.json entries emitted by the story-creator agent often carry only a
``name`` and a ``description``, while the rest of the pipeline addresses them
by short canonical ids taken from the director's shot list:

    characters:  output/characters/tick/tick_front.png
    locations:   output/locations/nana_repair_shop/nana_repair_shop.png

``resolve_id`` recovers those ids from shot_list.json, so a run whose story
schema drifted does not crash the reference generators.

Only used by generate_character_sheets.py / generate_location_refs.py; it
never writes back to story.json.
"""
import json
import re
from pathlib import Path


def slugify(text: str) -> str:
    """Lowercase, non-alphanumerics to '_', collapse runs, strip edges."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (text or "").lower())).strip("_")


def _tokens(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def reference_ids(run_dir, kind: str) -> set:
    """Canonical ids for 'characters' or 'locations' as used by shot_list.json."""
    shot_list = Path(run_dir) / "shot_list.json"
    if not shot_list.is_file():
        return set()
    try:
        data = json.loads(shot_list.read_text())
    except (OSError, json.JSONDecodeError):
        return set()

    ids = set()
    shots = data.get("shots") or []
    if kind == "characters":
        for shot in shots:
            for entry in shot.get("characters_in_frame") or []:
                if isinstance(entry, dict) and entry.get("character_id"):
                    ids.add(entry["character_id"])
    elif kind == "locations":
        for shot in shots:
            if shot.get("location_id"):
                ids.add(shot["location_id"])
        for scene in data.get("scenes") or []:
            ids.update(scene.get("location_ids") or [])

    # Fall back to the reference paths themselves if the structured fields
    # were absent from this run's shot list.
    if not ids:
        prefix = "characters" if kind == "characters" else "locations"
        for shot in shots:
            refs = (shot.get("keyframe_strategy") or {}).get("reference_images") or []
            for ref in refs:
                m = re.match(rf"output/{prefix}/([^/]+)/", str((ref or {}).get("source", "")))
                if m:
                    ids.add(m.group(1))
    return ids


def name_for_id(known_id: str, names) -> str:
    """Best display name for a canonical id, e.g. "nana_repair_shop"
    -> "Nana Cog's Repair Shop".

    Scores each candidate by the fraction of the id's underscore-separated
    tokens found in the name and keeps the highest (ties go to the shorter
    name). Below 0.6 the id is not really represented, so the titleised id is
    returned instead of a misleading match.
    """
    id_tokens = set(known_id.split("_"))
    best, best_score = None, 0.0
    for name in names:
        if not name:
            continue
        tokens = _tokens(name)
        if not tokens:
            continue
        score = len(id_tokens & tokens) / max(1, len(id_tokens))
        if score > best_score or (score == best_score and best and len(tokens) < len(_tokens(best))):
            best, best_score = name, score
    if not best or best_score < 0.6:
        return known_id.replace("_", " ").title()
    return best


def resolve_id(name: str, existing: str, known_ids: set) -> str:
    """Return ``existing`` unchanged, else match ``name`` against ``known_ids``.

    Matching order: exact slug -> best token overlap (>= 60% of the id's
    underscore-separated tokens present in the name, which is how
    "The Clockwork Moth (Tick)" maps to ``tick`` and "The Great Gear" maps to
    ``great_gear_chamber``) -> plain slug.
    """
    if existing:
        return existing
    slug = slugify(name)
    if not known_ids:
        return slug
    if slug in known_ids:
        return slug
    name_tokens = _tokens(name)
    best, best_score = None, 0.0
    for kid in known_ids:
        kid_tokens = set((kid or "").split("_"))
        if not kid_tokens:
            continue
        score = len(kid_tokens & name_tokens) / len(kid_tokens)
        if score > best_score:
            best, best_score = kid, score
    if best and best_score >= 0.6:
        return best
    return slug


def resolve_characters(characters: list, run_dir) -> list:
    """Fill in missing ``id`` / ``visual_description`` / ``type`` in place."""
    known = reference_ids(run_dir, "characters")
    for char in characters:
        if not isinstance(char, dict):
            continue
        char["id"] = resolve_id(char.get("name", ""), char.get("id", ""), known)
        if not char.get("visual_description"):
            char["visual_description"] = char.get("description") or char.get("name", "")
        if not char.get("type"):
            char["type"] = "magical mechanical creature" if is_creature(char) else "character"
    return characters


def is_creature(char: dict) -> bool:
    """True when the subject is not a humanoid (so standing poses would misfit)."""
    ctype = (char.get("type") or "").lower()
    if ctype:
        return "creature" in ctype or "mechanical" in ctype
    blob = f"{char.get('name', '')} {char.get('description', '')}".lower()
    return any(marker in blob for marker in ("creature", "wingspan", "clockwork moth"))
