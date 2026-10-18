"""Tests for pipeline/assemble.py, narration_check.py, subtitles.py and the validate_audio_timing
narration-repeat check.

Fixture: a run dir with three synthetic clips at a small, fps-agnostic rate (20 fps: one frame
is 0.05 s, so 0.25 s / 0.3 s / 1.4 s all land on the frame grid and the J/L numbers are exact).
Clip 2 carries a 1 s 440 Hz dialogue stem anchored at 1.4 s.  Decisions: a J-cut into clip 2
(audio_lead_s 0.25) and an L-cut out of it (a_out_s 0.3 s before the stem ends).

When ffmpeg is missing the clip files are placeholders and the probes are monkeypatched;
the tests that encode or decode with ffmpeg skip.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

PIPE = Path(__file__).resolve().parents[1]
ROOT = PIPE.parent
sys.path.insert(0, str(PIPE))
sys.path.insert(0, str(ROOT))

import assemble  # noqa: E402
import narration_check  # noqa: E402
import subtitles  # noqa: E402
import validate_audio_timing  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")

FPS = 20
CLIP_S = 4.0
CLIP_FRAMES = int(CLIP_S * FPS)
SR = assemble.SR
ANCHOR = 1.4
STEM_S = 1.0
LEAD = 0.25
LAG = 0.3
SHOTS = ["S01_001", "S01_002", "S01_003"]


def tone(seconds: float, hz: float = 440.0, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    x = (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    return np.stack([x, x], axis=1)


def make_clip(path: Path) -> None:
    if HAVE_FFMPEG:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate={FPS}",
                        "-t", str(CLIP_S), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", str(path)], check=True)
    else:
        path.write_bytes(b"placeholder")


def shot(sid: str, scene: str, dialogue=None) -> dict:
    return {"shot_id": sid, "scene_id": scene, "sequence_order": int(sid[-1]), "duration_seconds": CLIP_S,
            "edit": {"handle_head_s": 1.0, "handle_tail_s": 1.0, "cut_in": "x", "cut_out": "y"},
            "audio": {"dialogue": dialogue or [], "narration": None},
            "transition_to_next": {"type": "cut"}}


@pytest.fixture
def run(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "run"
    (d / "final").mkdir(parents=True)
    shots = [shot("S01_001", "scene_001"),
             shot("S01_002", "scene_001", [{"character_id": "maya", "line": "Hello there, little moth.", "start_time": ANCHOR}]),
             shot("S01_003", "scene_002")]
    (d / "shot_list.json").write_text(json.dumps({"shots": shots}), encoding="utf-8")
    for sid in SHOTS:
        cd = d / "clips" / sid
        cd.mkdir(parents=True)
        make_clip(cd / "clip_4k.mp4")
        (cd / "qa_verdict.json").write_text(json.dumps({"verdict": "PASS"}), encoding="utf-8")
    cd = d / "clips" / "S01_002"
    assemble.write_wav_f32(cd / "dialogue.wav", tone(STEM_S))
    (cd / "dialogue_meta.json").write_text(json.dumps({
        "status": "ok", "character_id": "maya", "text": "Hello there, little moth.", "anchor_s": ANCHOR,
        "duration_s": STEM_S, "lines": [{"text": "Hello there, little moth.", "start_s": ANCHOR, "duration_s": STEM_S}]}),
        encoding="utf-8")
    decisions = {"version": 2, "decisions": [
        {"boundary": "S01_001|S01_002", "action": "trim", "audio_lead_s": LEAD, "cut_type": "on-look", "note": "J-cut"},
        {"boundary": "S01_002|S01_003", "action": "trim", "a_out_s": ANCHOR + STEM_S - LAG, "note": "L-cut"}],
        "removed_clips": [], "inserted": []}
    (d / "final" / "edit_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    if not HAVE_FFMPEG:
        monkeypatch.setattr(assemble, "probe_frames", lambda p: CLIP_FRAMES)
        monkeypatch.setattr(assemble, "probe_fps", lambda p: float(FPS))
    return d


def expected_film_starts():
    n0 = round((CLIP_S - 0.5) * FPS) - round(0.5 * FPS)            # default trim: 0.5 .. dur-0.5
    n1 = round((ANCHOR + STEM_S - LAG) * FPS) - round((ANCHOR + LEAD) * FPS)
    n2 = n0
    return [n0, n1, n2], [0.0, n0 / FPS, (n0 + n1) / FPS]


# ----------------------------------------------------------------------------- timeline
def test_timeline_frames_and_starts(run):
    tl = assemble.build_timeline(run)
    frames, starts = expected_film_starts()
    assert tl["fps"] == FPS
    assert [s["n_frames"] for s in tl["segments"]] == frames
    assert [s["film_start_s"] for s in tl["segments"]] == pytest.approx(starts)
    assert tl["film_frames"] == sum(frames)
    assert [b["boundary"] for b in tl["boundaries"]] == ["S01_001|S01_002", "S01_002|S01_003"]
    assert all(b["source"] == "edit_decisions" for b in tl["boundaries"])


def test_stem_placement_j_and_l_cut(run):
    tl = assemble.build_timeline(run)
    seg = tl["segments"][1]
    assert seg["has_dialogue"]
    expected_at = seg["film_start_s"] + ANCHOR - seg["src_in_s"]
    assert seg["stem_at_s"] == pytest.approx(expected_at)
    assert int(round(seg["stem_at_s"] * SR)) == int(round(expected_at * SR))
    # J-cut: heard 0.25 s before the picture cut
    assert seg["film_start_s"] - seg["stem_at_s"] == pytest.approx(LEAD)
    assert seg["audio_lead_s"] == pytest.approx(LEAD)
    # L-cut: the stem runs 0.3 s past the cut and is not truncated
    assert seg["stem_end_s"] - seg["film_end_s"] == pytest.approx(LAG)
    assert seg["audio_lag_s"] == pytest.approx(LAG)
    assert not seg["lag_capped"]
    # and the mixed dialogue buffer really has energy there
    placements, voices, _ = assemble.build_placements(tl, run, {"no_music": True}, tl["warnings"])
    n = int(round(tl["film_frames"] * SR / FPS))
    res = assemble.render_mix(placements, n, assemble.load_audio, {})
    dlg = res["bufs"]["dialogue"][:, 0]
    i_at = int(round(seg["stem_at_s"] * SR))
    i_cut = int(round(seg["film_start_s"] * SR))
    i_end = int(round(seg["stem_end_s"] * SR))
    assert np.abs(dlg[i_at - 480:i_at]).max() == 0.0
    assert np.abs(dlg[i_at + 10:i_cut]).max() > 0.1            # sounding before the picture cut
    assert np.abs(dlg[int(round(seg["film_end_s"] * SR)) + 10:i_end - 10]).max() > 0.1   # past the L-cut
    assert np.abs(dlg[i_end + 10:i_end + 4800]).max() == 0.0
    assert voices[0]["lines"][0]["start_s"] == pytest.approx(seg["stem_at_s"], abs=1e-3)


def test_double_voice_at_boundary(run):
    cd = run / "clips" / "S01_003"
    assemble.write_wav_f32(cd / "dialogue.wav", tone(STEM_S, 330.0))
    (cd / "dialogue_meta.json").write_text(json.dumps({"status": "ok", "character_id": "nana", "text": "Hm.",
                                                       "anchor_s": 0.8, "duration_s": STEM_S, "lines": []}), encoding="utf-8")
    with pytest.raises(assemble.DecisionError, match=r"double voice at boundary S01_002\|S01_003"):
        assemble.build_timeline(run)


def test_lead_and_b_in_must_agree(run):
    dec = json.loads((run / "final" / "edit_decisions.json").read_text())
    dec["decisions"][0]["b_in_s"] = ANCHOR + LEAD + 0.2
    (run / "final" / "edit_decisions.json").write_text(json.dumps(dec))
    with pytest.raises(assemble.DecisionError, match="disagree"):
        assemble.build_timeline(run)


def test_unapproved_clip_is_listed(run):
    (run / "clips" / "S01_003" / "qa_verdict.json").write_text(json.dumps({"verdict": "FAIL"}))
    with pytest.raises(assemble.InputError, match="S01_003"):
        assemble.build_timeline(run)
    (run / "clips" / "S01_003" / "MANUAL_PASS").touch()
    assemble.build_timeline(run)


def test_legacy_master_name_and_mixed_fps(run, monkeypatch):
    cd = run / "clips" / "S01_003"
    (cd / "clip_4k.mp4").rename(cd / "clip_4k60.mp4")
    assert assemble.build_timeline(run)["segments"][2]["src"].endswith("clip_4k60.mp4")
    monkeypatch.setattr(assemble, "probe_fps", lambda p: 24.0 if "S01_003" in str(p) else float(FPS))
    with pytest.raises(assemble.InputError, match="S01_003"):
        assemble.build_timeline(run)


# ----------------------------------------------------------------------------- narration
def test_duplicate_narration_text_fails_narration_check(run):
    nd = run / "audio" / "narration"
    nd.mkdir(parents=True)
    text = "In the village of Tickhaven there lived a boy."
    meta = {"blocks": [{"id": "N1", "scene_id": "scene_001", "span_clips": ["S01_001"], "starts_on": "S01_001", "text": text, "file": "N1.wav"},
                       {"id": "N2", "scene_id": "scene_002", "span_clips": ["S01_003"], "starts_on": "S01_003", "text": text, "file": "N2.wav"}]}
    (nd / "narration_meta.json").write_text(json.dumps(meta))
    (nd / "tts_jobs.json").write_text(json.dumps({"jobs": [{"id": "N1", "text": text}, {"id": "N2", "text": text}]}))
    res = narration_check.check(run, rendered=False)
    assert any("duplicate narration text" in e and "N1" in e and "N2" in e for e in res["errors"])
    meta["blocks"][1]["text"] = "Except, perhaps, the things that truly mattered."
    meta["blocks"][1]["span_clips"] = ["S01_002"]
    meta["blocks"][1]["starts_on"] = "S01_002"
    (nd / "narration_meta.json").write_text(json.dumps(meta))
    res = narration_check.check(run, rendered=False)
    assert any("S01_002 has dialogue" in e for e in res["errors"])


def test_narration_overlap_with_stem_is_an_error(run):
    nd = run / "audio" / "narration"
    nd.mkdir(parents=True)
    assemble.write_wav_f32(nd / "N1.wav", tone(3.0, 220.0, 0.3))
    (nd / "narration_meta.json").write_text(json.dumps({"blocks": [
        {"id": "N1", "span_clips": ["S01_001"], "starts_on": "S01_001", "text": "Once upon a time.", "file": "N1.wav",
         "duration_s": 3.0, "status": "ok"}]}))
    with pytest.raises(assemble.DecisionError, match="narration N1 overlaps dialogue S01_002"):
        assemble.build_timeline(run)
    (nd / "narration_meta.json").write_text(json.dumps({"blocks": [
        {"id": "N1", "span_clips": ["S01_001"], "starts_on": "S01_001", "text": "Once upon a time.", "file": "N1.wav",
         "duration_s": 2.0, "status": "ok"}]}))
    tl = assemble.build_timeline(run)
    assert tl["narration"][0]["film_start_s"] == pytest.approx(0.4)


def test_validate_audio_timing_nulls_repeated_narration(tmp_path):
    narr = {"text": "A long narration passage.", "span_clips": ["S01_001", "S01_002"]}
    shots = [dict(shot("S01_001", "scene_001"), audio={"dialogue": [], "narration": dict(narr)}),
             dict(shot("S01_002", "scene_001"), audio={"dialogue": [], "narration": dict(narr)})]
    p = tmp_path / "shot_list.json"
    p.write_text(json.dumps({"shots": shots}))
    issues = validate_audio_timing.validate(str(p))
    assert any("narration dict repeated on S01_002" in i for i in issues)
    validate_audio_timing.validate(str(p), fix=True)
    fixed = json.loads(p.read_text())["shots"]
    assert fixed[0]["audio"]["narration"] is not None
    assert fixed[1]["audio"]["narration"] is None


# ----------------------------------------------------------------------------- mixing
def test_ducking_curve():
    mask = np.zeros(4 * SR, np.float32)
    mask[1 * SR:2 * SR] = 1.0
    g = assemble.gain_curve(mask, -6.0, -12.0)
    g_db = 20 * np.log10(g)
    assert g_db[1 * SR:2 * SR].max() <= -12.0 + 1e-3
    assert g_db[int(2.45 * SR)] == pytest.approx(-6.0, abs=1e-3)
    assert g_db[int(0.5 * SR)] == pytest.approx(-6.0, abs=1e-3)
    assert g_db[int(0.97 * SR)] < -6.0 - 1.0          # look-ahead attack already under way 30 ms before onset


def test_voice_mask_from_stem_energy():
    x = tone(1.0)
    x[:SR // 2] = 0.0
    m = assemble.voice_mask_from(x)
    assert m[:SR // 2 - 480].max() == 0.0
    assert m[SR // 2 + 480:].min() == 1.0


def test_crossfade_continuity_white_noise_bed(run):
    rng = np.random.default_rng(7)
    for sid in SHOTS:
        noise = (0.2 * rng.standard_normal((int(CLIP_S * SR), 2))).astype(np.float32)
        assemble.write_wav_f32(run / "clips" / sid / "sfx.wav", noise)
    tl = assemble.build_timeline(run)
    placements, _, _ = assemble.build_placements(tl, run, {"no_music": True}, tl["warnings"])
    sfx = [p for p in placements if p["layer"] == "sfx"]
    assert len(sfx) == 3 and all(p["fade_in"] and p["fade_out"] for p in sfx)
    n = int(round(tl["film_frames"] * SR / FPS))
    res = assemble.render_mix(sfx, n, assemble.load_audio, {})
    bed = res["bufs"]["sfx"][:, 0]
    # 10 ms RMS envelope; the statistic is the step between neighbouring envelope frames
    hop = SR // 100
    env = np.sqrt(np.mean(bed[: len(bed) // hop * hop].reshape(-1, hop) ** 2, axis=1))
    step = np.abs(np.diff(env))
    cuts = [int(b["film_t_s"] * 100) for b in tl["boundaries"]]
    near = np.zeros(len(step), bool)
    for c in cuts:
        near[max(0, c - 5):c + 5] = True
    edge = np.zeros(len(step), bool)
    edge[:60] = edge[-60:] = True                                   # fade-in / fade-out at the film ends
    assert step[near].max() < np.percentile(step[~near & ~edge], 99.9)
    # the same statistic DOES flag a hard cut (control): second clip's bed only, no fades
    seg = tl["segments"][1]
    hard = [dict(p, fade_in=None, fade_out=None, at_s=seg["film_start_s"], src_in_s=seg["src_in_s"], src_out_s=seg["src_out_s"])
            for p in sfx if p["shot_id"] == "S01_002"]
    res2 = assemble.render_mix(hard, n, assemble.load_audio, {})
    bed2 = res2["bufs"]["sfx"][:, 0]
    env2 = np.sqrt(np.mean(bed2[: len(bed2) // hop * hop].reshape(-1, hop) ** 2, axis=1))
    step2 = np.abs(np.diff(env2))
    assert step2[near].max() > np.percentile(step2[~near & ~edge], 99.9)


def test_place_does_not_truncate_at_segment_end():
    buf = np.zeros((SR, 2), np.float32)
    a, b = assemble.place(buf, tone(0.5), 0.8, 0.0)
    assert (a, b) == (int(0.8 * SR), SR)             # only the buffer's own end clips it
    assert np.abs(buf[int(0.8 * SR):]).max() > 0.4


# ----------------------------------------------------------------------------- CLI
def test_dry_run_output_parses(run, capsys):
    assert assemble.main(["--run-dir", str(run), "--dry-run", "--no-music"]) == 0
    out = capsys.readouterr().out
    rows = [ln for ln in out.splitlines() if re.match(r"^\s*\d+\s+S01_\d{3}\s", ln)]
    assert [r.split()[1] for r in rows] == SHOTS
    frames, starts = expected_film_starts()
    for r, nf, st in zip(rows, frames, starts):
        cols = r.split()
        assert int(cols[3]) - int(cols[2]) == nf
        assert float(cols[6]) == pytest.approx(st, abs=1e-3)
    assert "lead" in out and f"fps {FPS}" in out


def test_validate_decisions_exit_codes(run, capsys):
    assert assemble.main(["--run-dir", str(run), "--validate-decisions", "--no-music"]) == 0
    dec = json.loads((run / "final" / "edit_decisions.json").read_text())
    dec["decisions"][0]["audio_lead_s"] = 0.9
    (run / "final" / "edit_decisions.json").write_text(json.dumps(dec))
    assert assemble.main(["--run-dir", str(run), "--validate-decisions", "--no-music"]) == 1
    assert "S01_001|S01_002" in capsys.readouterr().err
    assert assemble.main(["--run-dir", str(run / "nope"), "--validate-decisions", "--no-music"]) == 2


@needs_ffmpeg
def test_full_assembly_and_subtitles(run, capsys):
    rc = assemble.main(["--run-dir", str(run), "--no-music"])
    assert rc == 0, capsys.readouterr()
    final = run / "final"
    alog = json.loads((final / "assembly_log.json").read_text())
    assert alog["status"] == "ok", alog
    frames, _ = expected_film_starts()
    assert alog["ffprobe_checks"]["frames"] == sum(frames)
    assert alog["ffprobe_checks"]["fps"] == FPS
    assert alog["layers"]["dialogue"] == 1
    assert (final / "movie_4k.mp4").is_file() and (final / "mix.wav").is_file() and (final / "concat.txt").is_file()
    tl = json.loads((final / "timeline.json").read_text())
    assert [s["n_frames"] for s in tl["segments"]] == frames
    for ln in (final / "concat.txt").read_text().splitlines():
        assert (final / ln.split("'")[1]).is_file()
    # second run: every segment and the mix come from the cache
    assert assemble.main(["--run-dir", str(run), "--no-music"]) == 0
    alog2 = json.loads((final / "assembly_log.json").read_text())
    assert all(s["cached"] for s in alog2["segments"]) and alog2["mix"]["cached"]
    # subtitles from the placements
    assert subtitles.main(["--run-dir", str(run)]) == 0
    srt = (final / "movie.srt").read_text(encoding="utf-8")
    assert "[MAYA]: Hello there, little moth." in srt
    m = re.search(r"(\d\d):(\d\d):(\d\d),(\d\d\d) -->", srt)
    start = int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
    assert start == pytest.approx(tl["segments"][1]["stem_at_s"] - 0.1, abs=0.002)
    ass = (final / "movie.ass").read_text(encoding="utf-8")
    assert "PlayResX: 3840" in ass and "Style: Narration,Georgia,96" in ass and "Dialogue: 0," in ass


# ----------------------------------------------------------------------------- music
def test_music_requires_resolved_cues_unless_no_music(run):
    tl = assemble.build_timeline(run)
    with pytest.raises(assemble.InputError, match="music_render.py --validate"):
        assemble.build_placements(tl, run, {}, tl["warnings"])
    pl, _, cues = assemble.build_placements(tl, run, {"no_music": True}, tl["warnings"])
    assert cues == [] and not [p for p in pl if p["layer"] in ("music", "bed")]


def test_music_cues_and_beds_from_resolved_file(run):
    tl = assemble.build_timeline(run)
    md = run / "music"
    (md / "cues").mkdir(parents=True)
    (md / "beds").mkdir()
    assemble.write_wav_f32(md / "cues" / "M01a.wav", tone(4.0, 110.0, 0.5))
    assemble.write_wav_f32(md / "cues" / "M01b.wav", tone(4.0, 220.0, 0.5))
    assemble.write_wav_f32(md / "beds" / "B01.wav", tone(1.0, 55.0, 0.5))      # shorter than its span: looped
    film_end = tl["film_duration_s"]
    resolved = {"music_level_db": -6, "bed_level_db": -18, "ref_lufs": -18,
                "cues": [{"id": "M01a", "film_start_s": 0.0, "film_end_s": 3.0, "crossfade_s": 1.0, "level_db": 0.0, "lufs": -24.0,
                          "transition_in": {"type": "fade", "s": 0.5}, "transition_out": {"type": "crossfade", "s": 1.0}},
                         {"id": "M01b", "film_start_s": 3.0, "film_end_s": film_end, "crossfade_s": 1.0, "level_db": -2.0, "lufs": -18.0,
                          "transition_in": {"type": "crossfade", "s": 1.0}, "transition_out": {"type": "fade", "s": 1.0}}],
                "beds": [{"id": "B01", "scene_id": "scene_001", "film_start_s": 0.0, "film_end_s": 3.45, "crossfade_s": 1.0, "level_db": 0.0}]}
    (md / "music_cues.resolved.json").write_text(json.dumps(resolved))
    pl, _, cues = assemble.build_placements(tl, run, {}, tl["warnings"])
    music = {p["id"]: p for p in pl if p["layer"] == "music"}
    assert set(music) == {"M01a", "M01b"} and [c["id"] for c in cues] == ["M01a", "M01b"]
    assert music["M01a"]["gain_db"] == pytest.approx(-6 + 6)          # -24 LUFS cue lifted to the -18 reference
    assert music["M01b"]["gain_db"] == pytest.approx(-6 - 2)
    assert music["M01a"]["fade_out"] == [2.5, 3.5, "equal_power"] and music["M01b"]["fade_in"] == [2.5, 3.5, "equal_power"]
    bed = next(p for p in pl if p["layer"] == "bed")
    assert bed["gain_db"] == -18 and bed["tile"] and bed["src_out_s"] > 1.0
    n = int(round(tl["film_frames"] * SR / FPS))
    res = assemble.render_mix(pl, n, assemble.load_audio, {})
    assert any("looped" in w for w in res["warnings"])
    seg = tl["segments"][1]
    mid = int(round((seg["stem_at_s"] + 0.5) * SR))
    quiet = res["bufs"]["music"][mid - 2400:mid + 2400, 0]
    free = res["bufs"]["music"][int(1.5 * SR):int(1.5 * SR) + 4800, 0]
    g_music = assemble.gain_curve(res["mask"], *assemble.DUCK["music"])
    assert 20 * np.log10(g_music[mid]) == pytest.approx(-12.0, abs=1e-3)
    assert 20 * np.log10(g_music[int(1.5 * SR)]) == pytest.approx(-6.0, abs=1e-3)
    assert np.abs(quiet).max() > 0 and np.abs(free).max() > 0
    assert res["ducking_s"]["music"] >= STEM_S
