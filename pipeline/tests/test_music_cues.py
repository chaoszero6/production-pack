"""Unit tests for pipeline/music_render.py — pure functions only, no network, no ComfyUI.

  python -m pytest pipeline/tests/test_music_cues.py -q
"""
import copy
import os
import sys

import pytest

PIPE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PIPE)

import music_render as mr  # noqa: E402
import sfx_render as sr  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────────────
def _seg(idx, shot, scene, a, b, dlg=False):
    return {"idx": idx, "shot_id": shot, "scene_id": scene, "film_start_s": a, "film_end_s": b, "has_dialogue": dlg}


@pytest.fixture
def timeline():
    segs = [
        _seg(0, "S01_001", "scene_001", 0.0, 12.0),
        _seg(1, "S01_002", "scene_001", 12.0, 24.5, True),
        _seg(2, "S01_003", "scene_001", 24.5, 36.0),
        _seg(3, "S01_004", "scene_001", 36.0, 48.2),
        _seg(4, "S02_001", "scene_002", 48.2, 70.0),
        _seg(5, "S02_002", "scene_002", 70.0, 90.0, True),
        _seg(6, "S03_001", "scene_003", 90.0, 110.0),
    ]
    scenes = [
        {"scene_id": "scene_001", "film_start_s": 0.0, "film_end_s": 48.2, "shots": ["S01_001", "S01_002", "S01_003", "S01_004"]},
        {"scene_id": "scene_002", "film_start_s": 48.2, "film_end_s": 90.0, "shots": ["S02_001", "S02_002"]},
        {"scene_id": "scene_003", "film_start_s": 90.0, "film_end_s": 110.0, "shots": ["S03_001"]},
    ]
    return {"fps": 24, "sr": 48000, "film_duration_s": 110.0, "segments": segs, "scenes": scenes}


def _cue(cid, scene, **kw):
    base = {"id": cid, "scene_id": scene, "mood": "gentle wonder", "tempo_bpm": 72, "intensity": 0.35,
            "instrumentation": "soft strings, celesta, harp", "genre_tags": ["orchestral", "film score"],
            "key": "D major", "time_signature": "4", "vocals": False,
            "transition_in": {"type": "fade", "s": 1.5}, "transition_out": {"type": "crossfade", "s": 2.5},
            "loopable": False, "tail_s": 3.0, "seed": 20260001}
    base.update(kw)
    return base


@pytest.fixture
def cues():
    return {
        "version": 1,
        "global": {"style_tags": "orchestral film score, pixar, warm", "key": "D major", "vocals": False,
                   "bed_level_db": -18, "music_level_db": -6},
        "cues": [
            _cue("M01", "scene_001", anchor={"start_clip": "S01_001", "end_clip": "S01_004"}),
            _cue("M02", "scene_002", anchor={"start_clip": "S02_001", "end_clip": "S02_002"}, seed=20260002,
                 mood="rising hope", intensity=0.6, transition_out={"type": "fade", "s": 2.0}),
        ],
        "beds": [{"id": "A01", "scene_id": "scene_001", "anchor": {"start_clip": "S01_001", "end_clip": "S01_004"},
                  "prompt": "night harbour ambience, gulls, soft wind", "engine": "acestep", "level_db": 0,
                  "seed": 20260101}],
        "silence": [{"scene_id": "scene_003", "reason": "the reveal plays dry"}],
    }


# ── resolution ────────────────────────────────────────────────────────────────
def test_anchors_resolve_to_film_time(cues, timeline):
    resolved, problems = mr.resolve_cues(cues, timeline)
    assert problems == []
    m01 = next(c for c in resolved["cues"] if c["id"] == "M01")
    assert m01["start_s"] == 0.0 and m01["end_s"] == 48.2
    m02 = next(c for c in resolved["cues"] if c["id"] == "M02")
    assert m02["start_s"] == 48.2 and m02["end_s"] == 90.0
    a01 = resolved["beds"][0]
    assert (a01["start_s"], a01["end_s"]) == (0.0, 48.2)
    assert resolved["film_duration_s"] == 110.0
    # assemble.py's key names ride along in the resolved sheet
    assert (m01["film_start_s"], m01["film_end_s"], m01["crossfade_s"], m01["level_db"]) == (0.0, 48.2, 2.5, 0.0)
    assert (resolved["music_level_db"], resolved["bed_level_db"], resolved["ref_lufs"]) == (-6, -18, -18.0)


def test_anchor_wins_over_start_end(cues, timeline):
    cues["cues"][0]["start_s"], cues["cues"][0]["end_s"] = 5.0, 20.0  # stale numbers — the anchor wins
    resolved, problems = mr.resolve_cues(cues, timeline)
    assert problems == []
    assert resolved["cues"][0]["start_s"] == 0.0 and resolved["cues"][0]["end_s"] == 48.2


def test_unknown_anchor_clip_fails(cues, timeline):
    cues["cues"][0]["anchor"]["end_clip"] = "S09_999"
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("S09_999" in p for p in problems)


# ── tiling / coverage ─────────────────────────────────────────────────────────
def test_gap_over_half_second_fails(cues, timeline):
    # replace the scene_001 cue by two numeric cues with a 1.0 s hole between them
    cues["cues"][0] = _cue("M01", "scene_001", start_s=0.0, end_s=20.0)
    cues["cues"].insert(1, _cue("M03", "scene_001", start_s=21.0, end_s=48.2, seed=3))
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("gap of 1.00s" in p and "M01" in p and "M03" in p for p in problems)


def test_small_gap_is_fine(cues, timeline):
    cues["cues"][0] = _cue("M01", "scene_001", start_s=0.0, end_s=20.0)
    cues["cues"].insert(1, _cue("M03", "scene_001", start_s=20.4, end_s=48.2, seed=3))
    _, problems = mr.resolve_cues(cues, timeline)
    assert problems == []


def test_overlap_only_by_crossfade(cues, timeline):
    cues["cues"][0] = _cue("M01", "scene_001", start_s=0.0, end_s=24.0, transition_out={"type": "crossfade", "s": 2.0})
    cues["cues"].insert(1, _cue("M03", "scene_001", start_s=22.0, end_s=48.2, seed=3))
    _, problems = mr.resolve_cues(cues, timeline)
    assert problems == []
    cues["cues"][1]["start_s"] = 18.0  # 6 s overlap, 2 s crossfade
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("overlap" in p for p in problems)


def test_scene_without_cue_or_silence_fails(cues, timeline):
    cues["silence"] = []
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("scene_003" in p and "neither a cue nor a silence entry" in p for p in problems)


def test_silence_and_cue_on_same_scene_fails(cues, timeline):
    cues["silence"].append({"scene_id": "scene_001", "reason": "oops"})
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("scene_001" in p and "silence" in p for p in problems)


# ── rules ─────────────────────────────────────────────────────────────────────
def test_vocals_true_fails(cues, timeline):
    cues["cues"][0]["vocals"] = True
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("M01" in p and "vocals must be false" in p for p in problems)
    cues["cues"][0]["vocals"] = False
    cues["global"]["vocals"] = True
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("global.vocals" in p for p in problems)


def test_prompt_mentioning_singing_fails(cues, timeline):
    cues["cues"][1]["instrumentation"] = "strings, a choir singing softly"
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("M02" in p and "never ask for vocals" in p for p in problems)


def test_tempo_intensity_transition_ranges(cues, timeline):
    cues["cues"][0]["tempo_bpm"] = 200
    cues["cues"][1]["intensity"] = 1.4
    cues["cues"][1]["transition_out"] = {"type": "wipe", "s": 1}
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("tempo_bpm" in p for p in problems)
    assert any("intensity" in p for p in problems)
    assert any("transition_out.type 'wipe'" in p for p in problems)


def test_short_cue_fails(cues, timeline):
    cues["cues"][0] = _cue("M01", "scene_001", start_s=0.0, end_s=6.0)
    cues["cues"].insert(1, _cue("M03", "scene_001", start_s=6.0, end_s=48.2, seed=3))
    _, problems = mr.resolve_cues(cues, timeline)
    assert any("M01" in p and "shorter than the 10s minimum" in p for p in problems)


# ── splitting ─────────────────────────────────────────────────────────────────
def test_200s_cue_is_split_in_two_with_2s_crossfade():
    segs = [_seg(0, "S01_001", "scene_001", 0.0, 200.0)]
    tl = {"film_duration_s": 200.0, "segments": segs,
          "scenes": [{"scene_id": "scene_001", "film_start_s": 0.0, "film_end_s": 200.0, "shots": ["S01_001"]}]}
    doc = {"global": {"vocals": False},
           "cues": [_cue("M01", "scene_001", anchor={"start_clip": "S01_001", "end_clip": "S01_001"}, seed=100)]}
    resolved, problems = mr.resolve_cues(doc, tl)
    assert problems == []
    ids = [c["id"] for c in resolved["cues"]]
    assert ids == ["M01a", "M01b"]
    a, b = resolved["cues"]
    assert a["parent_id"] == "M01" and b["parent_id"] == "M01"
    assert a["start_s"] == 0.0 and a["end_s"] == 100.0
    assert b["start_s"] == 98.0 and b["end_s"] == 200.0          # 2 s overlap = the crossfade
    assert a["transition_out"] == {"type": "crossfade", "s": 2.0}
    assert b["transition_in"] == {"type": "crossfade", "s": 2.0}
    assert a["seed"] == 100 and b["seed"] == 101
    assert a["transition_in"]["type"] == "fade"                  # outer edges keep the original transitions
    assert b["transition_out"]["type"] == "crossfade" and b["transition_out"]["s"] == 2.5
    assert all(mr.render_seconds(c) <= mr.MAX_CUE_S_DEFAULT for c in resolved["cues"])


def test_split_respects_max_cue_s():
    pieces = mr.split_cue({"id": "M07", "start_s": 10.0, "end_s": 310.0, "seed": 1,
                           "transition_in": {"type": "fade", "s": 1}, "transition_out": {"type": "fade", "s": 1}}, 100.0)
    assert [p["id"] for p in pieces] == ["M07a", "M07b", "M07c"]
    assert pieces[-1]["end_s"] == 310.0 and pieces[0]["start_s"] == 10.0


# ── tags ──────────────────────────────────────────────────────────────────────
def test_tag_builder_is_instrumental_and_never_says_vocals(cues):
    tags = mr.build_tags(cues["cues"][0], cues["global"])
    low = tags.lower()
    assert "instrumental" in low
    assert "vocals" not in low and "vocal" not in low
    assert "72 bpm" in low
    assert "soft strings" in low and "celesta" in low and "gentle wonder" in low
    assert "orchestral" in low and "pixar" in low
    assert "soft and gentle" in low                               # intensity 0.35
    # even a composer who writes "no vocals" into the mood gets it stripped
    tags2 = mr.build_tags(dict(cues["cues"][0], mood="tender, no vocals, hopeful"), {})
    assert "vocals" not in tags2.lower() and "tender" in tags2.lower() and "hopeful" in tags2.lower()


def test_intensity_words():
    assert "quiet" in mr.intensity_word(0.1)
    assert "climactic" in mr.intensity_word(0.95)


def test_bed_tags_strip_voices():
    t = mr.build_bed_tags({"prompt": "harbour at night, distant voices, gulls, wind"})
    assert "voices" not in t and "gulls" in t and "ambient" in t


def test_render_seconds_clamps():
    c = {"start_s": 0.0, "end_s": 48.2, "tail_s": 3.0, "transition_out": {"type": "crossfade", "s": 2.5}}
    assert mr.render_seconds(c) == pytest.approx(53.7)
    assert mr.render_seconds({"start_s": 0, "end_s": 4, "tail_s": 0, "transition_out": {"type": "hard", "s": 0}}) == 10.0
    assert mr.render_seconds({"start_s": 0, "end_s": 500, "tail_s": 0, "transition_out": {}}) == 180.0


# ── combos ────────────────────────────────────────────────────────────────────
def test_pick_combo_closest_match():
    assert mr.pick_combo(["3", "4", "6"], "4") == "4"
    assert mr.pick_combo(["3/4", "4/4", "6/8"], "4") == "4/4"
    assert mr.pick_combo(["", "C major", "D major", "D minor"], "D major") == "D major"
    assert mr.pick_combo(["", "C:maj", "D:maj", "D:min"], "D major") == "D:maj"
    assert mr.pick_combo(["", "C major"], "F# minor", "") == ""        # unknown key -> auto (empty)
    assert mr.pick_combo(["en", "zh", "ja"], "en", "en") == "en"


# ── graph ─────────────────────────────────────────────────────────────────────
def test_acestep_graph_node_set_and_links():
    wf = mr.acestep_graph("orchestral, instrumental", 53.7, 20260001, "music/cues/M01", bpm=72,
                          keyscale="D major", timesignature="4")
    types = {n["class_type"] for n in wf.values()}
    assert types == {"CheckpointLoaderSimple", "TextEncodeAceStepAudio1.5", "ConditioningZeroOut",
                     "ModelSamplingAuraFlow", "EmptyAceStep1.5LatentAudio", "KSampler", "VAEDecodeAudio", "SaveAudio"}
    by = {n["class_type"]: n["inputs"] for n in wf.values()}
    ck = next(k for k, n in wf.items() if n["class_type"] == "CheckpointLoaderSimple")
    enc = next(k for k, n in wf.items() if n["class_type"] == "TextEncodeAceStepAudio1.5")
    zero = next(k for k, n in wf.items() if n["class_type"] == "ConditioningZeroOut")
    shift = next(k for k, n in wf.items() if n["class_type"] == "ModelSamplingAuraFlow")
    lat = next(k for k, n in wf.items() if n["class_type"] == "EmptyAceStep1.5LatentAudio")
    ks = next(k for k, n in wf.items() if n["class_type"] == "KSampler")
    dec = next(k for k, n in wf.items() if n["class_type"] == "VAEDecodeAudio")
    assert by["CheckpointLoaderSimple"]["ckpt_name"] == "ace_step_1.5_turbo_aio.safetensors"
    assert by["TextEncodeAceStepAudio1.5"]["clip"] == [ck, 1]
    assert by["TextEncodeAceStepAudio1.5"]["lyrics"] == "[Instrumental]"
    assert by["TextEncodeAceStepAudio1.5"]["bpm"] == 72 and by["TextEncodeAceStepAudio1.5"]["duration"] == 53.7
    assert by["TextEncodeAceStepAudio1.5"]["keyscale"] == "D major" and by["TextEncodeAceStepAudio1.5"]["timesignature"] == "4"
    assert by["ConditioningZeroOut"]["conditioning"] == [enc, 0]
    assert by["ModelSamplingAuraFlow"] == {"model": [ck, 0], "shift": 3.0}
    assert by["EmptyAceStep1.5LatentAudio"] == {"seconds": 53.7, "batch_size": 1}
    k = by["KSampler"]
    assert k["model"] == [shift, 0] and k["positive"] == [enc, 0] and k["negative"] == [zero, 0]
    assert k["latent_image"] == [lat, 0] and k["seed"] == 20260001
    assert (k["steps"], k["cfg"], k["sampler_name"], k["scheduler"], k["denoise"]) == (8, 1.0, "euler", "simple", 1.0)
    assert by["VAEDecodeAudio"] == {"samples": [ks, 0], "vae": [ck, 2]}
    assert by["SaveAudio"] == {"audio": [dec, 0], "filename_prefix": "music/cues/M01"}
    assert len(wf) == 8


def test_mmaudio_graph_links():
    wf = sr.mmaudio_graph("sfx_S01_001.mp4", "footsteps, wind", 12.0, 7, "sfx/S01_001")
    by = {n["class_type"]: n["inputs"] for n in wf.values()}
    assert by["LoadVideo"]["file"] == "sfx_S01_001.mp4"
    assert by["GetVideoComponents"]["video"] == ["1", 0]
    assert by["MMAudioSampler"]["images"] == ["2", 0]
    assert by["MMAudioSampler"]["mmaudio_model"] == ["3", 0] and by["MMAudioSampler"]["feature_utils"] == ["4", 0]
    assert by["MMAudioSampler"]["steps"] == 25 and by["MMAudioSampler"]["cfg"] == 4.5
    assert by["MMAudioSampler"]["negative_prompt"] == sr.SFX_NEG
    assert by["MMAudioModelLoader"]["base_precision"] == "fp16" and by["MMAudioFeatureUtilsLoader"]["mode"] == "44k"
    assert "bigvgan_vocoder_model" not in by["MMAudioFeatureUtilsLoader"]
    assert by["SaveAudio"]["audio"] == ["5", 0]


def test_sfx_prompt_filters_vocal_clauses():
    p = sr.sfx_prompt(["footsteps on stone", "his voice echoing", "a hushed whisper", "gulls"],
                      ["harbour water lapping", "distant crowd chatter"])
    assert "footsteps on stone" in p and "gulls" in p and "harbour water lapping" in p
    assert "echoing" not in p and "whisper" not in p.split(". ")[0] and "chatter" not in p
    assert sr.sfx_prompt([], []).startswith("quiet room tone")


# ── hash / idempotence ────────────────────────────────────────────────────────
def test_cue_hash_is_stable_and_sensitive(cues, timeline):
    r1, _ = mr.resolve_cues(copy.deepcopy(cues), timeline)
    r2, _ = mr.resolve_cues(copy.deepcopy(cues), timeline)
    assert mr.cue_hash(r1["cues"][0]) == mr.cue_hash(r2["cues"][0])
    cues["cues"][0]["seed"] += 1
    r3, _ = mr.resolve_cues(cues, timeline)
    assert mr.cue_hash(r1["cues"][0]) != mr.cue_hash(r3["cues"][0])
