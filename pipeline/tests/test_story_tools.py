"""Tests for story_check.py, dialogue_patch.py and shot_list_patch.py.

    python -m pytest pipeline/tests/test_story_tools.py -q
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

PIPELINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PIPELINE not in sys.path:
    sys.path.insert(0, PIPELINE)

import dialogue_patch  # noqa: E402
import shot_list_patch  # noqa: E402
import story_check  # noqa: E402


# ----------------------------------------------------------------------------- fixtures
def good_story() -> dict:
    return {
        "title": "The Second Cup",
        "logline": "A girl climbs for her lost mother and learns to be found.",
        "theme": "courage is asking",
        "want": "reach the peaks",
        "need": "let Nana help",
        "stakes": "Nana's lamp goes out alone",
        "setups_payoffs": [{"setup": "silver pin", "payoff": "pin opens gate",
                            "scene_setup": "scene_001", "scene_payoff": "scene_002"}],
        "narration_script": {"narrator_voice": "elderly-male, dry warmth", "style": "time and place"},
        "characters": [
            {"id": "maya", "name": "Maya", "role": "protagonist",
             "voice_style": "child, 12, quick", "verbal_signature": "counts"},
            {"id": "nana", "name": "Nana", "role": "mentor",
             "voice_style": "elderly-female, slow", "verbal_signature": "child"},
        ],
        "locations": [{"id": "kitchen", "name": "Kitchen"}],
        "acts": [{"act_number": 1, "scenes": ["scene_001", "scene_002"]}],
        "scenes": [
            {"id": "scene_001", "turn": "from permission withheld to given",
             "pov_character": "maya", "audience_feels": "ache",
             "linger": "Nana's hands around the cup",
             "music_cue": {"mood": "silence", "intensity": 0, "start": "on the cup", "stop": "on the door"},
             "readable_props": {"clock": "hands at ten past five"},
             "dialogue": [
                 {"character_id": "nana", "line": "Fog's thick past the bridge.", "emotion": "flat"},
                 {"character_id": "maya", "line": "I know the bridge.", "emotion": "quiet"},
                 {"character_id": "nana", "line": "Your mother knew it too.", "emotion": "quiet"},
             ],
             "narration": {"text": "The second winter without her.", "timing": "opening"}},
            {"id": "scene_002", "turn": "from alone to found",
             "dialogue": [
                 {"character_id": "maya", "line": "The gate's open. Come on.", "emotion": "bright"},
             ],
             "narration": None},
        ],
    }


def good_shot_list() -> dict:
    def shot(sid, scene, lines=(), narr=None, first="generate"):
        return {
            "shot_id": sid, "scene_id": scene, "sequence_order": 0,
            "camera": {"shot_type": "medium"}, "duration_seconds": 6.0,
            "characters_in_frame": [], "location_id": "kitchen",
            "keyframe_strategy": {"mode": "composited_i2v", "first_frame_source": first},
            "audio": {"dialogue": [{"character_id": c, "line": l} for c, l in lines],
                      "narration": narr, "use_native_h3_audio": False},
            "edit": {"relation_to_prev": "continuous", "handle_head_s": 1.0, "handle_tail_s": 1.0,
                     "cut_in": "x", "cut_out": "y"},
        }
    return {"movie_title": "The Second Cup", "total_shots": 6, "shots": [
        shot("S01_001", "scene_001", narr={"text": "The second winter without her.",
                                           "span_clips": ["S01_001", "S01_002"]}),
        shot("S01_002", "scene_001", [("nana", "Fog's thick past the bridge.")]),
        shot("S01_003", "scene_001", [("maya", "I know the bridge.")], first="S01_002"),
        shot("S01_004", "scene_001", [("nana", "Your mother knew it too.")]),
        shot("S02_001", "scene_002", [("maya", "The gate's open.")]),
        shot("S02_002", "scene_002", [("maya", "Come on.")], first="S02_001"),
    ]}


def dump(path, data):
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return str(path)


# ----------------------------------------------------------------------------- story_check
def test_story_check_good_story(tmp_path):
    story = dump(tmp_path / "story.json", good_story())
    out = tmp_path / "story_check.json"
    assert story_check.main(["--story", story, "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["ok"] and rep["blocking"] == []
    assert rep["dialogue_share"] > 0.55


def test_story_check_narration_only_blocks_on_share(tmp_path):
    s = good_story()
    for sc in s["scenes"]:
        sc["dialogue"] = []
        sc["narration"] = {"text": "Long ago a girl wanted to climb and the narrator told you so."}
    out = tmp_path / "r.json"
    assert story_check.main(["--story", dump(tmp_path / "story.json", s), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert any(b["code"] == "dialogue_share" for b in rep["blocking"])
    assert rep["dialogue_share"] == 0.0


def test_story_check_over_length_line(tmp_path):
    s = good_story()
    s["scenes"][0]["dialogue"][0]["line"] = "word " * 40
    out = tmp_path / "r.json"
    assert story_check.main(["--story", dump(tmp_path / "story.json", s), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert any(b["code"] == "line_too_long" for b in rep["blocking"])


def test_story_check_empty_turn_blocks_missing_turn_warns(tmp_path):
    s = good_story()
    s["scenes"][0]["turn"] = ""
    del s["scenes"][1]["turn"]
    out = tmp_path / "r.json"
    assert story_check.main(["--story", dump(tmp_path / "story.json", s), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert [b["code"] for b in rep["blocking"]] == ["empty_turn"]
    assert any(w["code"] == "no_turn_field" for w in rep["warnings"])


def test_story_check_narrator_class_shared(tmp_path):
    s = good_story()
    s["characters"][1]["voice_style"] = "elderly-male, slow"
    out = tmp_path / "r.json"
    assert story_check.main(["--story", dump(tmp_path / "story.json", s), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert any(b["code"] == "narrator_class" and b["character"] == "nana" for b in rep["blocking"])


def test_story_check_with_shots_accepts_split_and_reports_missing(tmp_path):
    story = dump(tmp_path / "story.json", good_story())
    sl = good_shot_list()
    out = tmp_path / "r.json"
    # split line ("The gate's open." + "Come on.") is accepted
    assert story_check.main(["--story", story, "--shots", dump(tmp_path / "sl.json", sl), "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["missing_lines"] == [] and rep["dialogue_share_shots"] > 0.55
    # drop a line and invent narration → blocked, missing line named
    sl["shots"][3]["audio"]["dialogue"] = []
    sl["shots"][4]["audio"]["narration"] = {"text": "She felt very brave that morning."}
    assert story_check.main(["--story", story, "--shots", dump(tmp_path / "sl.json", sl), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert [m["line"] for m in rep["missing_lines"]] == ["Your mother knew it too."]
    assert any(b["code"] == "invented_narration" for b in rep["blocking"])


def test_story_check_bad_input(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert story_check.main(["--story", str(p)]) == 2


# ----------------------------------------------------------------------------- dialogue_patch
def test_dialogue_patch_applies_good_drops_speaker_change(tmp_path, capsys):
    story = dump(tmp_path / "story.json", good_story())
    patch = dump(tmp_path / "patch.json", {"lines": [
        {"scene_id": "scene_001", "index": 1, "character_id": "maya",
         "line": "I know the bridge, Nana.", "emotion": "not looking up"},
        {"scene_id": "scene_001", "index": 2, "character_id": "maya",  # speaker is nana
         "line": "Stolen line.", "emotion": "x"},
        {"scene_id": "scene_001", "index": 9, "character_id": "nana", "line": "No such index."},
        {"scene_id": "scene_002", "index": 0, "character_id": "maya", "line": "word " * 40},
    ], "notes": ["test"]})
    diff = tmp_path / "diff.txt"
    assert dialogue_patch.main([story, patch, "--diff", str(diff), "--backup"]) == 0
    s = json.loads((tmp_path / "story.json").read_text())
    d = s["scenes"][0]["dialogue"]
    assert d[1]["line"] == "I know the bridge, Nana." and d[1]["emotion"] == "not looking up"
    assert d[2]["line"] == "Your mother knew it too."           # speaker-changing entry dropped
    assert s["scenes"][1]["dialogue"][0]["line"] == "The gate's open. Come on."  # too long dropped
    err = capsys.readouterr().out
    assert "speaker changed" in err and "index out of range" in err and "too long" in err
    assert (tmp_path / "story_v2_before.json").exists()
    assert "+ I know the bridge, Nana." in diff.read_text(encoding="utf-8")


def test_dialogue_patch_nothing_applicable_exits_1(tmp_path):
    story = dump(tmp_path / "story.json", good_story())
    patch = dump(tmp_path / "patch.json", {"lines": [
        {"scene_id": "scene_001", "index": 0, "character_id": "maya", "line": "x"}]})
    assert dialogue_patch.main([story, patch, "--diff", str(tmp_path / "d.txt")]) == 1
    assert json.loads((tmp_path / "story.json").read_text()) == good_story()


def test_dialogue_patch_bad_input(tmp_path):
    story = dump(tmp_path / "story.json", good_story())
    patch = dump(tmp_path / "patch.json", {"nope": 1})
    assert dialogue_patch.main([story, patch]) == 2


# ----------------------------------------------------------------------------- shot_list_patch
def _new_shot(sid, scene, lines=()):
    s = good_shot_list()["shots"][1]
    s = copy.deepcopy(s)
    s["shot_id"], s["scene_id"] = sid, scene
    s["audio"]["dialogue"] = [{"character_id": c, "line": l} for c, l in lines]
    s["edit"]["relation_to_prev"] = "reaction"
    return s


def test_shot_list_patch_replace_insert_delete_and_renumber(tmp_path):
    sl_path = dump(tmp_path / "shot_list.json", good_shot_list())
    replaced = _new_shot("S01_004", "scene_001", [("nana", "Your mother knew it too.")])
    replaced["duration_seconds"] = 8.0
    patch = dump(tmp_path / "patch.json", {
        "patch_version": 1,
        "replace": [replaced],
        "insert_after": [{"after": "S01_001", "shot": _new_shot("S01_001a", "scene_001")}],
        "delete": [],
        "notes": "linger reaction after the establishing shot",
    })
    rep = tmp_path / "rep.json"
    assert shot_list_patch.main([sl_path, patch, "--renumber", "--report", str(rep)]) == 0
    sl = json.loads((tmp_path / "shot_list.json").read_text())
    ids = [s["shot_id"] for s in sl["shots"]]
    assert ids == ["S01_001", "S01_002", "S01_003", "S01_004", "S01_005", "S02_001", "S02_002"]
    assert [s["sequence_order"] for s in sl["shots"]] == list(range(1, 8))
    # the inserted shot is S01_002 now; old S01_002 → S01_003 etc.
    assert sl["shots"][1]["edit"]["relation_to_prev"] == "reaction"
    # old S01_002 is now S01_003: the narration span follows the map
    assert sl["shots"][0]["audio"]["narration"]["span_clips"] == ["S01_001", "S01_003"]
    report = json.loads(rep.read_text())
    assert report["ok"] and report["inserted"] == ["S01_001a"] and report["replaced"] == ["S01_004"]
    assert report["renumber_map"] == {"S01_001a": "S01_002", "S01_002": "S01_003",
                                      "S01_003": "S01_004", "S01_004": "S01_005"}
    # references were rewritten through the map: first_frame_source S01_002 → S01_003, S02_001 unchanged
    assert sl["shots"][3]["keyframe_strategy"]["first_frame_source"] == "S01_003"
    assert sl["shots"][6]["keyframe_strategy"]["first_frame_source"] == "S02_001"
    assert sl["shots"][4]["duration_seconds"] == 8.0
    assert sl["total_shots"] == 7


def test_shot_list_patch_span_clips_rewritten(tmp_path):
    base = good_shot_list()
    base["shots"][0]["audio"]["narration"]["span_clips"] = ["S01_001", "S01_002", "S01_003"]
    sl_path = dump(tmp_path / "shot_list.json", base)
    patch = dump(tmp_path / "patch.json", {"patch_version": 1,
                                           "insert_after": [{"after": "S01_001", "shot": _new_shot("S01_001a", "scene_001")}]})
    assert shot_list_patch.main([sl_path, patch, "--renumber"]) == 0
    sl = json.loads((tmp_path / "shot_list.json").read_text())
    assert sl["shots"][0]["audio"]["narration"]["span_clips"] == ["S01_001", "S01_003", "S01_004"]


def test_shot_list_patch_delete_keeps_lines(tmp_path):
    sl_path = dump(tmp_path / "shot_list.json", good_shot_list())
    # delete S01_004 but move its line into S01_003 → accepted
    moved = _new_shot("S01_003", "scene_001", [("maya", "I know the bridge."), ("nana", "Your mother knew it too.")])
    patch = dump(tmp_path / "patch.json", {"patch_version": 1, "replace": [moved], "delete": ["S01_004"]})
    assert shot_list_patch.main([sl_path, patch]) == 0
    sl = json.loads((tmp_path / "shot_list.json").read_text())
    assert [s["shot_id"] for s in sl["shots"]] == ["S01_001", "S01_002", "S01_003", "S02_001", "S02_002"]


def test_shot_list_patch_dropped_line_rejected(tmp_path, capsys):
    original = good_shot_list()
    sl_path = dump(tmp_path / "shot_list.json", original)
    patch = dump(tmp_path / "patch.json", {"patch_version": 1, "delete": ["S01_004"]})
    assert shot_list_patch.main([sl_path, patch]) == 1
    assert "Your mother knew it too." in capsys.readouterr().out
    assert json.loads((tmp_path / "shot_list.json").read_text()) == original  # nothing written


def test_shot_list_patch_rejects_unknown_ids_and_invalid_shot(tmp_path):
    sl_path = dump(tmp_path / "shot_list.json", good_shot_list())
    bad = _new_shot("S01_002", "scene_001", [("nana", "Fog's thick past the bridge.")])
    del bad["edit"]
    patch = dump(tmp_path / "patch.json", {"patch_version": 1, "replace": [bad], "delete": ["S09_001"]})
    assert shot_list_patch.main([sl_path, patch]) == 1
    assert shot_list_patch.validate_shot(bad) == ["missing 'edit'"]


def test_shot_list_patch_renumber_refuses_when_clip_dir_exists(tmp_path):
    sl_path = dump(tmp_path / "shot_list.json", good_shot_list())
    (tmp_path / "clips" / "S01_003").mkdir(parents=True)
    patch = dump(tmp_path / "patch.json", {"patch_version": 1,
                                           "insert_after": [{"after": "S01_001", "shot": _new_shot("S01_001a", "scene_001")}]})
    assert shot_list_patch.main([sl_path, patch, "--renumber", "--run-dir", str(tmp_path)]) == 1
    assert json.loads((tmp_path / "shot_list.json").read_text()) == good_shot_list()
    # without --renumber the same patch applies (ids stay, S01_001a kept)
    assert shot_list_patch.main([sl_path, patch, "--run-dir", str(tmp_path)]) == 0
    ids = [s["shot_id"] for s in json.loads((tmp_path / "shot_list.json").read_text())["shots"]]
    assert ids[1] == "S01_001a"


def test_shot_list_patch_bad_input(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("[", encoding="utf-8")
    assert shot_list_patch.main([str(p), str(p)]) == 2
