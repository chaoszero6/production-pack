#!/usr/bin/env python3
"""assemble.py — cut the approved clips into one picture and mix every audio element once.

THE TIMELINE MODEL
==================
Clip files contribute PICTURE ONLY.  Every audio element — dialogue stems, narration blocks,
music cues, ambience beds, per-clip SFX, optional native H3 ambience — is placed exactly once on
ONE film timeline and mixed once.  That removes the three defects of per-clip audio: audio jumps
at every cut (clips arrived in three different audio states), double voice (stems muxed twice,
narration rendered per clip, native H3 voices under the narration) and the impossibility of
J/L cuts.  A J-cut or an L-cut is now just an offset of the stem relative to the picture cut.

Film time is built from FRAME COUNTS, never from float durations:

    fps          probed from the masters (clips/<SID>/clip_4k.mp4; legacy clip_4k60.mp4);
                 every master must share one fps
    src_in_f     = round(cut_in_s  * fps)             first master frame used
    n_frames     = round(cut_out_s * fps) - src_in_f  frames used
    film_start_s = (sum of n_frames of every earlier segment) / fps

Default trim when the editor made no decision for a boundary: src_in = handle_head_s / 2,
src_out = master_duration - handle_tail_s / 2, clamped on dialogue clips so the stem lies
inside the picture with 0.2 s on each side.  `final/edit_decisions.json` (v2 or the legacy flat
list) overrides per boundary "A|B":

    a_out_s       where shot A leaves (source time of A)
    b_in_s        where shot B enters (source time of B)
    audio_lead_s  J-cut: B's stem is heard this long BEFORE the picture cut.  The stem is always
                  placed at  B.film_start + (anchor_s - b_in_s), so b_in_s == anchor_s + lead
                  (checked to one frame when both are given).  lead <= 0.5 s.
    audio_lag_s   L-cut: A's stem is NOT truncated at the cut; it runs (anchor+stem) - a_out_s
                  past it, capped at 0.6 s with a 40 ms fade at the cap.

Voices never overlap: stem A must end 0.10 s before stem B starts ("double voice at boundary").
Narration is placed once per block at film_start(starts_on) + 0.4 s (or the block's offset_s)
and must not overlap any stem (`--narration-fit` may speed it up to 1.08x via atempo).

THE MIX
=======
float32 stereo at 48 kHz, five buffers (dialogue, narration, music, bed, sfx) and a mono voice
mask built from the stems' own energy (RMS per 10 ms > -45 dBFS), never from clip audio.
`place()` never truncates at segment ends.  Beds and SFX cross the cuts with an equal-power
crossfade of 2*ov around every boundary (ov = max(0.18, lead, lag)).  Music cues are normalised
to one LUFS (sidecar music/cues/<id>.json "lufs"), faded per transition_in/out and crossfaded
equal-power between consecutive cues.  Side-chain ducking runs at 100 Hz control rate with a
60 ms (look-ahead) attack and a 400 ms release: music -6 -> -12 dB, bed -18 -> -24, sfx -12 -> -20.
Sum, true-peak guard, final/mix_raw.wav, two-pass loudnorm (I=-16, TP=-1.5, LRA=11) ->
final/mix.wav (48 kHz stereo pcm_s24le), then mux onto final/picture.mp4 -> final/movie_4k.mp4.

OUTPUTS   final/timeline.json  final/segments/*.mp4(+.json cache)  final/concat.txt
          final/picture.mp4  final/mix_raw.wav  final/mix.wav  final/movie_4k.mp4
          final/audio_placements.json (for subtitles.py)  final/assembly_log.json
          final/movie_4k_master.mov with --master-mov

EXIT      0 ok · 1 validation/decision error or failed output check (message names the
          boundary/block) · 2 bad input (missing file, unapproved clip, mixed fps) · 3 ffmpeg
          failure.  Nothing falls back silently: every "could not" is an error or a warning
          recorded in assembly_log.json.

MUSIC (music/music_cues.resolved.json, written by pipeline/music_render.py --validate)
    {"music_level_db": -6, "bed_level_db": -18, "ref_lufs": -18,
     "cues": [{"id": "M01a", "film_start_s": 0.0, "film_end_s": 42.5, "crossfade_s": 2.0,
               "level_db": 0.0, "lufs": -18.2,
               "transition_in": {"type": "fade", "s": 1.5},         # fade|crossfade|hard|none
               "transition_out": {"type": "crossfade", "s": 2.0}}],
     "beds": [{"id": "B01", "scene_id": "scene_001", "film_start_s": 0.0, "film_end_s": 42.5,
               "crossfade_s": 1.0, "level_db": 0.0}]}
    The id is only the file stem: music/cues/<id>.wav, music/beds/<id>.wav (a bed shorter than
    its span is looped, warned); a cue's lufs may also come from music/cues/<id>.json.  Without
    the resolved file assembly stops (exit 2) unless --no-music is passed.  Shot-relative
    (start_shot/end_shot) and scene_id spans are still understood for hand-written files.

Usage: assemble.py --run-dir D [--timeline-only] [--dry-run] [--validate-decisions]
       [--encoder libx264|h264_nvenc] [--keep-native-ambience] [--no-music] [--no-sfx]
       [--force-segments] [--force-mix] [--master-mov] [--narration-fit] [--sfx-mute-under-voice]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))

SR = 48000
CTRL_HZ = 100                       # ducking control rate
HOP = SR // CTRL_HZ                 # 480 samples per control frame
VOICE_GAP_S = 0.10                  # minimum silence between two voices
LEAD_MAX_S = 0.5
LAG_CAP_S = 0.6
LAG_FADE_S = 0.04
STEM_PAD_S = 0.2                    # default clamp: picture around the stem
NARR_OFFSET_S = 0.4
NARR_FIT_MAX = 1.08
OV_MIN_S = 0.18
BED_XFADE_S = 1.0
MASK_THRESH_DBFS = -45.0
DUCK = {                            # layer: (base_db, under_voice_db)
    "music": (-6.0, -12.0),
    "bed": (-18.0, -24.0),
    "sfx": (-12.0, -20.0),
}
NATIVE_BED_DB = -18.0
NARRATION_DB = -1.0
ATTACK_S = 0.06
RELEASE_S = 0.40
PEAK_GUARD = 0.89
MUSIC_REF_LUFS = -18.0

EXIT_OK, EXIT_DECISION, EXIT_INPUT, EXIT_FFMPEG = 0, 1, 2, 3


class AssemblyError(Exception):
    code = EXIT_DECISION


class DecisionError(AssemblyError):
    code = EXIT_DECISION


class InputError(AssemblyError):
    code = EXIT_INPUT


class FfmpegError(AssemblyError):
    code = EXIT_FFMPEG


def log(msg: str) -> None:
    print(f"[assemble {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------------------- ffmpeg helpers
def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run_ff(args: list[str], what: str) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FfmpegError(f"ffmpeg failed ({what}): {r.stderr.strip()[-800:]}\n  cmd: {' '.join(cmd)}")
    return r


def _own_probe_frames(path: str) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise InputError(f"ffprobe could not count frames of {path}: {out.stderr.strip()[-300:]}")
    return int(out.stdout.strip().split(",")[0])


try:  # reuse the pipeline's probe when its dependencies are present
    from upscale_clip import probe_frames as _probe_frames  # type: ignore
except Exception:  # noqa: BLE001 — requests missing, etc.
    _probe_frames = _own_probe_frames


def probe_frames(path: str) -> int:
    try:
        return int(_probe_frames(str(path)))
    except AssemblyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise InputError(f"ffprobe could not count frames of {path}: {exc}") from exc


def probe_packets(path: str) -> int:
    """Video packet count (== frame count for one-frame-per-packet codecs); fast on 4K files."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise InputError(f"ffprobe could not count packets of {path}: {out.stderr.strip()[-300:]}")
    return int(out.stdout.strip().split(",")[0])


def probe_streams(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=index,codec_type,r_frame_rate,avg_frame_rate,sample_rate,channels,duration,nb_frames"
                          ":format=duration", "-of", "json", str(path)], capture_output=True, text=True)
    if out.returncode != 0:
        raise InputError(f"ffprobe failed on {path}: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout or "{}")


def probe_fps(path: str) -> float:
    info = probe_streams(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
            den = float(den or 1)
            fps = float(num) / den if den else 0.0
            if fps <= 0:
                raise InputError(f"{path}: cannot read the frame rate ({s.get('r_frame_rate')})")
            return round(fps, 3)
    raise InputError(f"{path}: no video stream")


def fps_arg(fps: float) -> str:
    """ffmpeg -r value for a probed fps (NTSC rates exactly, else the number)."""
    for exact, frac in ((23.976, "24000/1001"), (29.97, "30000/1001"), (59.94, "60000/1001")):
        if abs(fps - exact) < 0.01:
            return frac
    return f"{fps:g}"


def probe_duration(path: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise InputError(f"ffprobe could not read the duration of {path}") from exc


def has_audio_stream(path: str) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe_streams(path).get("streams", []))


def load_audio_ffmpeg(path: str, sr: int = SR) -> np.ndarray:
    """Decode any ffmpeg-readable file to float32 stereo (n, 2) at `sr`."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        raise InputError(f"cannot decode audio from {path}: {r.stderr.decode(errors='replace')[-300:]}")
    x = np.frombuffer(r.stdout, dtype=np.float32)
    return x.reshape(-1, 2).copy()


# ----------------------------------------------------------------------------- WAV I/O (no ffmpeg)
def write_wav_f32(path: Path, x: np.ndarray, sr: int = SR) -> None:
    """IEEE float32 WAV writer; x is (n, ch) or (n,)."""
    if x.ndim == 1:
        x = x[:, None]
    ch = x.shape[1]
    data = x.astype("<f4").tobytes()
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 3, ch, sr, sr * ch * 4, ch * 4, 32)
    hdr += b"data" + struct.pack("<I", len(data))
    Path(path).write_bytes(hdr + data)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Minimal RIFF reader (PCM 16/24/32, float32) -> ((n, ch) float32, sr)."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise InputError(f"{path}: not a RIFF/WAVE file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise InputError(f"{path}: missing fmt/data chunk")
    tag, ch, sr, _, _, bits = fmt
    if tag == 3 and bits == 32:
        x = np.frombuffer(data, dtype="<f4").astype(np.float32)
    elif tag in (1, 0xFFFE) and bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif tag in (1, 0xFFFE) and bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif tag in (1, 0xFFFE) and bits == 32:
        x = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise InputError(f"{path}: unsupported WAV format tag {tag} / {bits} bit")
    return x.reshape(-1, ch), sr


def load_audio(path: str, sr: int = SR) -> np.ndarray:
    """Default loader: ffmpeg when present (handles mp4, 44.1k stems, resampling); else a
    native WAV read that insists on the target sample rate (no resampling without ffmpeg)."""
    if have_ffmpeg():
        return load_audio_ffmpeg(path, sr)
    x, fs = read_wav(Path(path))
    if fs != sr:
        raise InputError(f"{path}: {fs} Hz but ffmpeg is not available to resample to {sr} Hz")
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    return x[:, :2].astype(np.float32)


# ----------------------------------------------------------------------------- small helpers
def db_to_lin(db: float) -> float:
    return float(10 ** (db / 20.0))


def load_json(path: Path, required: bool = False, what: str = "") -> dict | list | None:
    if not path.is_file():
        if required:
            raise InputError(f"missing required file: {path}" + (f" ({what})" if what else ""))
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{path}: invalid JSON ({exc})") from exc


def file_sig(path: Path) -> dict:
    st = path.stat()
    return {"mtime": round(st.st_mtime, 3), "size": st.st_size}


def sha1_of(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


# ----------------------------------------------------------------------------- shot list
def shots_in_order(shot_list: dict | list) -> list[dict]:
    """Director order: scenes in order of first appearance, sequence_order inside a scene
    (file order when sequence_order is absent)."""
    shots = shot_list.get("shots", shot_list) if isinstance(shot_list, dict) else shot_list
    if isinstance(shots, dict):
        shots = [dict(s, shot_id=s.get("shot_id") or sid) for sid, s in shots.items()]
    scene_rank: dict = {}
    keyed = []
    for i, s in enumerate(shots):
        if not isinstance(s, dict) or not (s.get("shot_id") or s.get("id")):
            raise InputError(f"shot_list.json: shot #{i} has no shot_id")
        s = dict(s)
        s["shot_id"] = s.get("shot_id") or s.get("id")
        sc = s.get("scene_id", s.get("scene"))
        scene_rank.setdefault(sc, len(scene_rank))
        so = s.get("sequence_order")
        keyed.append(((scene_rank[sc], float(so) if isinstance(so, (int, float)) else float(i), i), s))
    keyed.sort(key=lambda t: t[0])
    return [s for _, s in keyed]


def clip_master(clip_dir: Path) -> Path | None:
    for name in ("clip_4k.mp4", "clip_4k60.mp4"):
        p = clip_dir / name
        if p.is_file():
            return p
    return None


def clip_approved(clip_dir: Path) -> tuple[bool, str]:
    if (clip_dir / "MANUAL_PASS").exists():
        return True, "MANUAL_PASS"
    v = load_json(clip_dir / "qa_verdict.json")
    if not isinstance(v, dict):
        return False, "no qa_verdict.json"
    verdict = str(v.get("verdict", "")).strip().upper()
    return verdict == "PASS", f"verdict {verdict or '?'}"


def dialogue_for(clip_dir: Path, shot: dict) -> dict:
    """{'has': bool, 'stem': path|None, 'anchor_s', 'stem_dur_s', 'meta'}; raises when the shot
    list promises dialogue the clip cannot deliver."""
    meta = load_json(clip_dir / "dialogue_meta.json")
    stem = clip_dir / "dialogue.wav"
    lines = [d for d in ((shot.get("audio") or {}).get("dialogue") or []) if isinstance(d, dict)
             and re.search(r"[A-Za-z0-9]", str(d.get("line") or d.get("text") or ""))]
    sid = shot["shot_id"]
    if meta is None:
        if lines:
            raise InputError(f"{sid}: shot list has dialogue but clips/{sid}/dialogue_meta.json is missing")
        return {"has": False, "stem": None, "anchor_s": None, "stem_dur_s": 0.0, "meta": None}
    status = meta.get("status", "ok")
    if status == "no_speech":
        return {"has": False, "stem": None, "anchor_s": None, "stem_dur_s": 0.0, "meta": meta}
    if status != "ok":
        raise InputError(f"{sid}: dialogue_meta.json status is '{status}' — render the stem before assembly")
    if not stem.is_file():
        raise InputError(f"{sid}: dialogue_meta.json says ok but clips/{sid}/dialogue.wav is missing")
    try:
        anchor = float(meta["anchor_s"])
        dur = float(meta["duration_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InputError(f"{sid}: dialogue_meta.json needs numeric anchor_s and duration_s ({exc})") from exc
    return {"has": True, "stem": stem, "anchor_s": anchor, "stem_dur_s": dur, "meta": meta}


OFFSET_RE = re.compile(r"(?:stem\s+offset|offset\s+(?:the\s+)?stem|shift\s+(?:the\s+)?stem)[^0-9+\-]{0,40}([+-]?\d+(?:\.\d+)?)\s*(?:s\b|sec)"
                       r"|([+-]?\d+(?:\.\d+)?)\s*(?:s\b|sec)\w*\s+stem\s+offset", re.I)


def stem_offsets_from_fixes(run_dir: Path) -> tuple[dict[str, float], list[str]]:
    """assembly_fixes.json -> {shot_id: stem_offset_s}.  Numeric `stem_offset_s` on the clip entry
    or on a fix wins; otherwise the fix text is parsed for '+0.80s stem offset' wording."""
    fixes = load_json(run_dir / "assembly_fixes.json")
    offsets: dict[str, float] = {}
    notes: list[str] = []
    if not isinstance(fixes, dict):
        return offsets, notes
    for sid, entry in (fixes.get("clips") or {}).items():
        if not isinstance(entry, dict):
            continue
        val = entry.get("stem_offset_s")
        if not isinstance(val, (int, float)):
            for fx in entry.get("fixes") or []:
                if isinstance(fx, dict) and isinstance(fx.get("stem_offset_s"), (int, float)):
                    val = fx["stem_offset_s"]
                    break
                text = " ".join(str(fx.get(k, "")) for k in ("fix", "prompt_fix", "rule")) if isinstance(fx, dict) else str(fx)
                m = OFFSET_RE.search(text)
                if m:
                    val = float(m.group(1) or m.group(2))
                    break
        if isinstance(val, (int, float)) and val != 0:
            offsets[sid] = float(val)
            notes.append(f"{sid}: stem_offset_s {val:+.3f} from assembly_fixes.json")
    return offsets, notes


# ----------------------------------------------------------------------------- decisions
def load_decisions(path: Path) -> dict:
    """Normalise edit_decisions.json (v2 dict or legacy flat list) to
    {"decisions": {boundary: d}, "removed_clips": [...], "inserted": [...]}."""
    raw = load_json(path)
    out: dict = {"decisions": {}, "removed_clips": [], "inserted": [], "present": raw is not None}
    if raw is None:
        return out
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("decisions") or []
        out["removed_clips"] = list(raw.get("removed_clips") or [])
        out["inserted"] = list(raw.get("inserted") or [])
        if not isinstance(items, list):
            raise DecisionError(f"{path}: 'decisions' must be a list")
    else:
        raise DecisionError(f"{path}: unrecognised format")
    for d in items:
        if not isinstance(d, dict) or not d.get("boundary"):
            raise DecisionError(f"{path}: decision without a 'boundary': {d}")
        b = str(d["boundary"])
        if "|" not in b:
            raise DecisionError(f"{path}: boundary '{b}' must be 'A|B'")
        if b in out["decisions"]:
            raise DecisionError(f"{path}: boundary {b} decided twice")
        action = str(d.get("action") or "trim").lower()
        if action not in ("trim", "insert", "hold", "request_rerender"):
            raise DecisionError(f"boundary {b}: unknown action '{action}'")
        for k in ("a_out_s", "b_in_s", "audio_lead_s", "audio_lag_s"):
            if d.get(k) is not None and not isinstance(d[k], (int, float)):
                raise DecisionError(f"boundary {b}: {k} must be a number")
        out["decisions"][b] = dict(d, action=action)
    for ins in out["inserted"]:
        if not isinstance(ins, dict) or not ins.get("after") or not ins.get("shot_id"):
            raise DecisionError(f"{path}: inserted entry needs 'after' and 'shot_id': {ins}")
    return out


# ----------------------------------------------------------------------------- timeline
def build_timeline(run_dir: Path, probe_frames_fn=None, probe_fps_fn=None, narration_fit: bool = False,
                   decisions_path: Path | None = None) -> dict:
    """Validate every input and compute the film timeline (frame math only).

    probe_frames_fn / probe_fps_fn can be injected (tests without ffmpeg)."""
    run_dir = Path(run_dir)
    pf = probe_frames_fn or probe_frames
    pfps = probe_fps_fn or probe_fps
    warnings: list[str] = []
    shot_list = load_json(run_dir / "shot_list.json", required=True, what="Director shot list")
    shots = shots_in_order(shot_list)
    by_id = {s["shot_id"]: s for s in shots}
    dec = load_decisions(decisions_path or (run_dir / "final" / "edit_decisions.json"))

    # ---- play order: removals and inserts
    removed = set(dec["removed_clips"])
    order: list[tuple[str, dict | None]] = [(s["shot_id"], None) for s in shots if s["shot_id"] not in removed]
    for sid in removed:
        if sid not in by_id:
            raise DecisionError(f"removed_clips names unknown shot {sid}")
        warnings.append(f"{sid}: removed by edit_decisions.json")
    for ins in dec["inserted"]:
        if ins["shot_id"] not in by_id:
            raise DecisionError(f"inserted shot {ins['shot_id']} is not in shot_list.json")
        pos = next((i for i, (sid, _) in enumerate(order) if sid == ins["after"]), None)
        if pos is None:
            raise DecisionError(f"inserted after unknown/removed shot {ins['after']}")
        order.insert(pos + 1, (ins["shot_id"], ins))
    for b, d in dec["decisions"].items():
        if d["action"] == "insert" and d.get("insert_clip"):
            a, _ = b.split("|", 1)
            if d["insert_clip"] not in by_id:
                raise DecisionError(f"boundary {b}: insert_clip {d['insert_clip']} is not in shot_list.json")
            pos = next((i for i, (sid, _) in enumerate(order) if sid == a), None)
            if pos is None:
                raise DecisionError(f"boundary {b}: shot {a} is not in the play order")
            order.insert(pos + 1, (d["insert_clip"], {"after": a, "shot_id": d["insert_clip"],
                                                      "src_in_s": d.get("insert_in_s"), "src_out_s": d.get("insert_out_s")}))
    if not order:
        raise InputError("no shots to assemble")

    # ---- approval + masters + fps
    problems: list[str] = []
    masters: dict[str, Path] = {}
    for sid, _ in order:
        cdir = run_dir / "clips" / sid
        m = clip_master(cdir)
        if m is None:
            problems.append(f"{sid}: no clips/{sid}/clip_4k.mp4 (nor legacy clip_4k60.mp4)")
            continue
        ok, why = clip_approved(cdir)
        if not ok:
            problems.append(f"{sid}: not approved ({why}; touch clips/{sid}/MANUAL_PASS to accept)")
        masters[sid] = m
    if problems:
        raise InputError("cannot assemble — " + str(len(problems)) + " clip(s) blocked:\n  " + "\n  ".join(problems))
    fps_by: dict[str, float] = {}
    frames_by: dict[str, int] = {}
    for sid, m in masters.items():
        if sid in fps_by:
            continue
        fps_by[sid] = float(pfps(str(m)))
        frames_by[sid] = int(pf(str(m)))
        if frames_by[sid] <= 0:
            raise InputError(f"{sid}: master {m} has no frames")
    fps_counts: dict[float, list[str]] = {}
    for sid, f in fps_by.items():
        fps_counts.setdefault(f, []).append(sid)
    if len(fps_counts) > 1:
        majority = max(fps_counts.items(), key=lambda kv: len(kv[1]))[0]
        odd = [f"{sid} ({f} fps)" for f, sids in fps_counts.items() if f != majority for sid in sids]
        raise InputError(f"masters do not share one frame rate (majority {majority} fps); odd ones: " + ", ".join(odd))
    fps = next(iter(fps_counts))
    if fps <= 0:
        raise InputError("frame rate probe returned 0")
    frame_s = 1.0 / fps

    offsets, offset_notes = stem_offsets_from_fixes(run_dir)
    warnings.extend(offset_notes)

    # ---- per-segment defaults
    segs: list[dict] = []
    for idx, (sid, ins) in enumerate(order):
        shot = by_id[sid]
        edit = shot.get("edit") or {}
        master = masters[sid]
        n_master = frames_by[sid]
        dur = n_master * frame_s
        dlg = dialogue_for(run_dir / "clips" / sid, shot)
        head = float(edit.get("handle_head_s") or 0.0)
        tail = float(edit.get("handle_tail_s") or 0.0)
        stem_offset = offsets.get(sid, 0.0)
        eff_anchor = (dlg["anchor_s"] + stem_offset) if dlg["has"] else None
        if ins is not None:
            if ins.get("src_in_s") is None or ins.get("src_out_s") is None:
                raise DecisionError(f"inserted {sid} after {ins['after']} needs src_in_s and src_out_s")
            src_in, src_out = float(ins["src_in_s"]), float(ins["src_out_s"])
            has_dlg = False          # cutaways never carry a voice
            if dlg["has"]:
                warnings.append(f"{sid} (inserted after {ins['after']}): clip has a dialogue stem; cutaways carry no voice")
        else:
            src_in, src_out = head / 2.0, dur - tail / 2.0
            has_dlg = dlg["has"]
            if has_dlg:
                src_in = max(0.0, min(src_in, eff_anchor - STEM_PAD_S))
                want_out = eff_anchor + dlg["stem_dur_s"] + STEM_PAD_S
                src_out = max(src_out, want_out)
                if src_out > dur + 1e-9:
                    warnings.append(f"{sid}: stem ends {want_out - dur:.2f} s after the master — picture cannot pad it; "
                                    f"the stem tail runs over the next cut")
                    src_out = dur
        segs.append({
            "idx": idx, "shot_id": sid, "scene_id": shot.get("scene_id", shot.get("scene")),
            "src": str(master), "master_frames": n_master, "master_dur_s": round(dur, 6),
            "src_in_s": src_in, "src_out_s": src_out, "default_in_s": src_in, "default_out_s": src_out,
            "has_dialogue": has_dlg, "anchor_s": dlg["anchor_s"], "stem_dur_s": dlg["stem_dur_s"],
            "stem_offset_s": stem_offset, "stem_file": str(dlg["stem"]) if has_dlg else None,
            "audio_lead_s": 0.0, "audio_lag_s": 0.0, "lag_capped": False, "inserted": ins is not None,
            "transition_in": "cut", "decided_in": False, "decided_out": False,
            "character_id": (dlg["meta"] or {}).get("character_id") if dlg["meta"] else None,
            "warnings": [],
        })

    # ---- boundaries: decisions override defaults
    boundaries: list[dict] = []
    seen_bounds = set()
    for a, b in zip(segs, segs[1:]):
        key = f"{a['shot_id']}|{b['shot_id']}"
        seen_bounds.add(key)
        d = dec["decisions"].get(key)
        trans = (by_id[a["shot_id"]].get("transition_to_next") or {})
        ttype = (trans.get("type") if isinstance(trans, dict) else trans) or "cut"
        b["transition_in"] = str(ttype)
        if str(ttype).lower() not in ("cut", "match-cut", "none", ""):
            warnings.append(f"boundary {key}: transition_to_next '{ttype}' is logged only — straight cut rendered (xfade is a follow-up)")
        source = "default"
        lead = lag = 0.0
        if d is not None:
            source = "edit_decisions"
            if d["action"] == "request_rerender":
                raise DecisionError(f"boundary {key}: editor requested a re-render — assembly cannot proceed on this boundary")
            a_out = d.get("a_out_s")
            b_in = d.get("b_in_s")
            lead_d = d.get("audio_lead_s")
            lag_d = d.get("audio_lag_s")
            # J-cut into B
            if lead_d is not None and float(lead_d) > 0:
                if not b["has_dialogue"]:
                    raise DecisionError(f"boundary {key}: audio_lead_s on a boundary into {b['shot_id']}, which has no dialogue stem")
                if float(lead_d) > LEAD_MAX_S + 1e-9:
                    raise DecisionError(f"boundary {key}: audio_lead_s {lead_d} exceeds the {LEAD_MAX_S} s maximum")
            b_anchor = (b["anchor_s"] + b["stem_offset_s"]) if b["has_dialogue"] else None
            if lead_d is not None and b_in is not None and b["has_dialogue"]:
                if abs(float(b_in) - (b_anchor + float(lead_d))) > frame_s + 1e-6:
                    raise DecisionError(f"boundary {key}: b_in_s {b_in} and audio_lead_s {lead_d} disagree "
                                        f"(anchor {b_anchor:.3f}: b_in_s must equal anchor_s + audio_lead_s within one frame)")
            if b_in is None and lead_d is not None and b["has_dialogue"]:
                b_in = b_anchor + float(lead_d)
            if b_in is not None:
                b["src_in_s"] = float(b_in)
                b["decided_in"] = True
            if b["has_dialogue"]:
                lead = max(0.0, b["src_in_s"] - b_anchor)
                if lead > LEAD_MAX_S + 1e-9:
                    raise DecisionError(f"boundary {key}: b_in_s {b['src_in_s']:.3f} puts {b['shot_id']}'s stem {lead:.2f} s "
                                        f"before the cut (max lead {LEAD_MAX_S} s)")
            # L-cut out of A
            if lag_d is not None and float(lag_d) > 0 and not a["has_dialogue"]:
                raise DecisionError(f"boundary {key}: audio_lag_s on a boundary out of {a['shot_id']}, which has no dialogue stem")
            if a["has_dialogue"]:
                a_stem_end = a["anchor_s"] + a["stem_offset_s"] + a["stem_dur_s"]
                if a_out is None and lag_d is not None and float(lag_d) > 0:
                    a_out = a_stem_end - float(lag_d)
                if a_out is not None:
                    a["src_out_s"] = float(a_out)
                    a["decided_out"] = True
                    lag = max(0.0, a_stem_end - a["src_out_s"])
                    if lag_d is not None and float(lag_d) > 0 and abs(float(lag_d) - lag) > frame_s + 1e-6:
                        raise DecisionError(f"boundary {key}: a_out_s {a_out} and audio_lag_s {lag_d} disagree "
                                            f"(stem ends at {a_stem_end:.3f}: lag must equal stem end - a_out_s within one frame)")
            elif a_out is not None:
                a["src_out_s"] = float(a_out)
                a["decided_out"] = True
        boundaries.append({"boundary": key, "source": source, "audio_lead_s": round(lead, 4), "audio_lag_s": round(lag, 4),
                           "cut_type": (d or {}).get("cut_type"), "note": (d or {}).get("note")})
        b["audio_lead_s"] = round(lead, 4)
        a["audio_lag_s"] = round(lag, 4)
    for key in dec["decisions"]:
        if key not in seen_bounds:
            raise DecisionError(f"boundary {key}: decided but the two shots are not consecutive in the play order")

    # ---- frame math + film positions
    film_f = 0
    for s in segs:
        if s["src_in_s"] < -1e-9:
            raise DecisionError(f"{s['shot_id']}: src_in {s['src_in_s']:.3f} s is before the clip starts")
        src_in_f = int(round(s["src_in_s"] * fps))
        src_out_f = int(round(s["src_out_s"] * fps))
        if src_out_f > s["master_frames"]:
            raise DecisionError(f"{s['shot_id']}: src_out {s['src_out_s']:.3f} s ({src_out_f} f) exceeds the master "
                                f"({s['master_frames']} f = {s['master_dur_s']:.3f} s)")
        n = src_out_f - src_in_f
        if n <= 0:
            raise DecisionError(f"{s['shot_id']}: empty segment (in {s['src_in_s']:.3f} s >= out {s['src_out_s']:.3f} s)")
        s["src_in_f"], s["n_frames"] = src_in_f, n
        s["src_in_s"], s["src_out_s"] = round(src_in_f / fps, 6), round(src_out_f / fps, 6)
        s["film_start_f"], s["film_start_s"] = film_f, round(film_f / fps, 6)
        film_f += n
        s["film_end_f"], s["film_end_s"] = film_f, round(film_f / fps, 6)
        if s["has_dialogue"]:
            eff = s["anchor_s"] + s["stem_offset_s"]
            s["stem_at_s"] = round(s["film_start_s"] + (eff - s["src_in_s"]), 6)
            keep = s["stem_dur_s"]
            if eff + keep > s["src_out_s"] + LAG_CAP_S + 1e-9:      # L-cut lag cap: 0.6 s past the cut, 40 ms fade
                s["lag_capped"] = True
                keep = (s["src_out_s"] + LAG_CAP_S) - eff
                s["warnings"].append(f"stem runs {eff + s['stem_dur_s'] - s['src_out_s']:.2f} s past the cut — "
                                     f"capped at {LAG_CAP_S} s with a {int(LAG_FADE_S * 1000)} ms fade")
            s["stem_keep_s"] = round(keep, 6)
            s["stem_end_s"] = round(s["stem_at_s"] + keep, 6)
            if s["stem_at_s"] < s["film_start_s"] - LEAD_MAX_S - 1e-6:
                raise DecisionError(f"{s['shot_id']}: stem starts {s['film_start_s'] - s['stem_at_s']:.2f} s before its picture")
        else:
            s["stem_at_s"] = s["stem_end_s"] = s["stem_keep_s"] = None
    film_duration = film_f / fps
    # lead / lag as REALISED on the frame grid (b_in_s rounds to a frame, so a 0.25 s lead at 24 fps is 6 frames)
    for bd, (a, b) in zip(boundaries, zip(segs, segs[1:])):
        lead = max(0.0, b["film_start_s"] - b["stem_at_s"]) if b["has_dialogue"] else 0.0
        lag = max(0.0, a["stem_end_s"] - a["film_end_s"]) if a["has_dialogue"] else 0.0
        b["audio_lead_s"], a["audio_lag_s"] = round(lead, 4), round(lag, 4)
        bd.update({"film_t_s": b["film_start_s"], "film_f": b["film_start_f"], "a_out_s": a["src_out_s"], "b_in_s": b["src_in_s"],
                   "audio_lead_s": round(lead, 4), "audio_lag_s": round(lag, 4)})

    # ---- scenes
    scenes: list[dict] = []
    for s in segs:
        if scenes and scenes[-1]["scene_id"] == s["scene_id"]:
            scenes[-1]["shots"].append(s["shot_id"])
            scenes[-1]["film_end_s"] = s["film_end_s"]
        else:
            scenes.append({"scene_id": s["scene_id"], "film_start_s": s["film_start_s"], "film_end_s": s["film_end_s"],
                           "shots": [s["shot_id"]]})

    # ---- narration blocks (film positions resolved, overlap checked in validate_voices)
    narration = narration_blocks(run_dir, segs, warnings)

    tl = {"fps": fps, "sr": SR, "film_duration_s": round(film_duration, 6), "film_frames": film_f,
          "segments": segs, "boundaries": boundaries, "scenes": scenes, "narration": narration,
          "decisions_file": str(decisions_path or (run_dir / "final" / "edit_decisions.json")) if dec["present"] else None,
          "warnings": warnings}
    validate_voices(tl, narration_fit=narration_fit, run_dir=run_dir)
    return tl


def narration_blocks(run_dir: Path, segs: list[dict], warnings: list[str]) -> list[dict]:
    meta = load_json(run_dir / "audio" / "narration" / "narration_meta.json")
    if meta is None:
        return []
    blocks = meta.get("blocks") if isinstance(meta, dict) else meta
    if not isinstance(blocks, list):
        raise InputError("audio/narration/narration_meta.json: 'blocks' must be a list")
    start_of = {}
    end_of = {}
    for s in segs:
        start_of.setdefault(s["shot_id"], s["film_start_s"])
        end_of[s["shot_id"]] = s["film_end_s"]
    out = []
    for i, b in enumerate(blocks):
        bid = str(b.get("id") or f"N{i + 1}")
        if b.get("status") not in (None, "ok"):
            raise InputError(f"narration block {bid}: status '{b.get('status')}' — render it before assembly")
        fname = b.get("file") or f"{bid}.wav"
        path = Path(fname) if os.path.isabs(str(fname)) else run_dir / "audio" / "narration" / fname
        if not path.is_file():
            raise InputError(f"narration block {bid}: audio file missing ({path})")
        dur = b.get("duration_s")
        if not isinstance(dur, (int, float)):
            raise InputError(f"narration block {bid}: duration_s missing — run tts_render / narration_check --rendered")
        span = list(b.get("span_clips") or [])
        starts_on = b.get("starts_on") or (span[0] if span else None)
        if not starts_on:
            raise InputError(f"narration block {bid}: no starts_on / span_clips")
        if starts_on not in start_of:
            raise InputError(f"narration block {bid}: starts_on {starts_on} is not in the play order")
        off = b.get("offset_s")
        at = start_of[starts_on] + (float(off) if isinstance(off, (int, float)) else NARR_OFFSET_S)
        span_end = max((end_of[c] for c in span if c in end_of), default=end_of[starts_on])
        if at + float(dur) > span_end + 0.5:
            warnings.append(f"narration {bid}: ends {at + float(dur) - span_end:.2f} s after its span ({span_end:.2f} s)")
        out.append({"id": bid, "scene_id": b.get("scene_id"), "file": str(path), "text": b.get("text", ""),
                    "duration_s": float(dur), "span_clips": span, "starts_on": starts_on,
                    "film_start_s": round(at, 6), "film_end_s": round(at + float(dur), 6), "fitted": False})
    return out


def voice_items(tl: dict) -> list[dict]:
    items = [{"kind": "dialogue", "id": s["shot_id"], "start": s["stem_at_s"], "end": s["stem_end_s"]}
             for s in tl["segments"] if s["has_dialogue"]]
    items += [{"kind": "narration", "id": n["id"], "start": n["film_start_s"], "end": n["film_end_s"]}
              for n in tl.get("narration", [])]
    return sorted(items, key=lambda v: (v["start"], v["end"]))


def validate_voices(tl: dict, narration_fit: bool = False, run_dir: Path | None = None) -> None:
    """Voices never overlap: previous end + 0.10 s <= next start."""
    for _attempt in range(2):
        items = voice_items(tl)
        clash = None
        for p, n in zip(items, items[1:]):
            if p["end"] + VOICE_GAP_S > n["start"] + 1e-6:
                clash = (p, n)
                break
        if clash is None:
            return
        p, n = clash
        if p["kind"] == "dialogue" and n["kind"] == "dialogue":
            raise DecisionError(f"double voice at boundary {p['id']}|{n['id']}: stem {p['id']} ends at {p['end']:.3f} s, "
                                f"stem {n['id']} starts at {n['start']:.3f} s (need {VOICE_GAP_S} s of silence)")
        narr = p if p["kind"] == "narration" else n
        other = n if narr is p else p
        block = next(b for b in tl["narration"] if b["id"] == narr["id"])
        if narration_fit and narr is p and not block["fitted"] and run_dir is not None:
            avail = other["start"] - VOICE_GAP_S - block["film_start_s"]
            tempo = block["duration_s"] / avail if avail > 0 else 99.0
            if 1.0 < tempo <= NARR_FIT_MAX:
                fit_narration(block, tempo, run_dir)
                tl["warnings"].append(f"narration {block['id']}: atempo {tempo:.3f} applied to clear {other['kind']} {other['id']}")
                continue
            raise DecisionError(f"narration {narr['id']} overlaps {other['kind']} {other['id']} "
                                f"({narr['start']:.2f}-{narr['end']:.2f} vs {other['start']:.2f}-{other['end']:.2f}); "
                                f"needs atempo {tempo:.3f} > {NARR_FIT_MAX} — shorten the block or move its span")
        raise DecisionError(f"narration {narr['id']} overlaps {other['kind']} {other['id']} "
                            f"({narr['start']:.2f}-{narr['end']:.2f} s vs {other['start']:.2f}-{other['end']:.2f} s)"
                            + ("" if narration_fit else " — use --narration-fit (<= 1.08x) or move the span"))
    raise DecisionError("narration overlap persists after --narration-fit")


def fit_narration(block: dict, tempo: float, run_dir: Path) -> None:
    out_dir = Path(run_dir) / "final" / "narration_fit"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{block['id']}.wav"
    run_ff(["-i", block["file"], "-af", f"atempo={tempo:.5f}", "-ar", str(SR), dest], f"narration fit {block['id']}")
    block["file"] = str(dest)
    block["duration_s"] = probe_duration(str(dest))
    block["film_end_s"] = round(block["film_start_s"] + block["duration_s"], 6)
    block["fitted"] = True
    block["tempo"] = round(tempo, 5)


def dry_run_table(tl: dict, cues: list[dict] | None = None) -> str:
    cues = cues or []
    hdr = f"{'idx':>3} {'shot':<9} {'in_f':>6} {'out_f':>6} {'in_s':>8} {'out_s':>8} {'film_in':>9} {'film_out':>9} {'stem_at':>8} {'lead':>5} {'lag':>5}  cues / warnings"
    rows = [f"fps {tl['fps']}  film {tl['film_duration_s']:.3f} s  {tl['film_frames']} frames", hdr, "-" * len(hdr)]
    for s in tl["segments"]:
        covering = [c["id"] for c in cues if c["film_start_s"] < s["film_end_s"] and c["film_end_s"] > s["film_start_s"]]
        stem = f"{s['stem_at_s']:8.3f}" if s["has_dialogue"] else f"{'-':>8}"
        extra = ",".join(covering) or "-"
        if s["warnings"]:
            extra += "  ! " + "; ".join(s["warnings"])
        rows.append(f"{s['idx']:>3} {s['shot_id']:<9} {s['src_in_f']:>6} {s['src_in_f'] + s['n_frames']:>6} {s['src_in_s']:8.3f} "
                    f"{s['src_out_s']:8.3f} {s['film_start_s']:9.3f} {s['film_end_s']:9.3f} {stem} {s['audio_lead_s']:5.2f} "
                    f"{s['audio_lag_s']:5.2f}  {extra}")
    for n in tl.get("narration", []):
        rows.append(f"    narration {n['id']:<6} {n['film_start_s']:9.3f} -> {n['film_end_s']:9.3f}  on {n['starts_on']}")
    for w in tl["warnings"]:
        rows.append(f"    warning: {w}")
    return "\n".join(rows)


# ----------------------------------------------------------------------------- picture
def encode_segment(seg: dict, out_dir: Path, fps: float, encoder: str, force: bool, log_entry: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{seg['idx']:03d}_{seg['shot_id']}"
    dest, side = out_dir / f"{name}.mp4", out_dir / f"{name}.json"
    src = Path(seg["src"])
    sig = {"src": str(src), "src_mtime": round(src.stat().st_mtime, 3), "src_size": src.stat().st_size,
           "src_in_f": seg["src_in_f"], "n_frames": seg["n_frames"], "encoder": encoder, "fps": fps}
    if not force and dest.is_file() and side.is_file():
        try:
            prev = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = None
        if prev == sig:
            log_entry.update({"cached": True})
            return dest
    seek = max(0.0, (seg["src_in_f"] - 0.5) / fps)      # half a frame early: the first frame kept is src_in_f
    vcodec = (["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "16", "-rc", "vbr"] if encoder == "h264_nvenc"
              else ["-c:v", "libx264", "-preset", "slow", "-crf", "16"])
    run_ff(["-ss", f"{seek:.6f}", "-i", src, "-frames:v", seg["n_frames"], "-an", *vcodec, "-pix_fmt", "yuv420p",
            "-r", fps_arg(fps), "-video_track_timescale", str(int(round(fps * 1000))), dest], f"segment {name}")
    got = probe_frames(str(dest))
    if got != seg["n_frames"]:
        raise FfmpegError(f"segment {name}: encoded {got} frames, expected {seg['n_frames']}")
    side.write_text(json.dumps(sig, indent=1), encoding="utf-8")
    log_entry.update({"cached": False})
    return dest


def build_picture(tl: dict, final: Path, encoder: str, force: bool, alog: dict) -> Path:
    seg_dir = final / "segments"
    paths = []
    for seg in tl["segments"]:
        entry = {"idx": seg["idx"], "shot_id": seg["shot_id"], "frames": seg["n_frames"], "seconds": round(seg["n_frames"] / tl["fps"], 4)}
        p = encode_segment(seg, seg_dir, tl["fps"], encoder, force, entry)
        entry["file"] = str(p.relative_to(final))
        alog["segments"].append(entry)
        paths.append(p)
        log(f"segment {seg['idx']:03d} {seg['shot_id']}: {seg['n_frames']} f {'(cached)' if entry['cached'] else '(encoded)'}")
    concat = final / "concat.txt"
    concat.write_text("".join(f"file '{p.relative_to(final).as_posix()}'\n" for p in paths), encoding="utf-8")
    picture = final / "picture.mp4"
    run_ff(["-f", "concat", "-safe", "0", "-i", concat, "-c:v", "copy", "-movflags", "+faststart", picture], "picture concat")
    got = probe_packets(str(picture))
    if got != tl["film_frames"]:
        raise FfmpegError(f"picture.mp4 has {got} frames, timeline expects {tl['film_frames']}")
    return picture


# ----------------------------------------------------------------------------- placements
def placement(layer: str, pid: str, file: str, at_s: float, gain_db: float = 0.0, src_in_s: float = 0.0,
              src_out_s: float | None = None, fade_in=None, fade_out=None, voice: bool = False, **extra) -> dict:
    """fade_in / fade_out: [t0_film_s, t1_film_s, 'equal_power'|'linear'] windows in film time."""
    d = {"layer": layer, "id": pid, "file": str(file), "at_s": round(float(at_s), 6), "gain_db": float(gain_db),
         "src_in_s": round(float(src_in_s), 6), "src_out_s": None if src_out_s is None else round(float(src_out_s), 6),
         "fade_in": fade_in, "fade_out": fade_out, "voice": voice}
    d.update(extra)
    return d


def build_placements(tl: dict, run_dir: Path, opts: dict, warnings: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    """Every audio element of the film as a placement; returns (placements, voices for subtitles, cue spans)."""
    run_dir = Path(run_dir)
    segs = tl["segments"]
    film_end = tl["film_duration_s"]
    pl: list[dict] = []
    voices: list[dict] = []

    # ---- dialogue stems (never truncated at the cut; capped L-cut gets a 40 ms fade)
    for s in segs:
        if not s["has_dialogue"]:
            continue
        fade_out = [s["stem_end_s"] - LAG_FADE_S, s["stem_end_s"], "linear"] if s["lag_capped"] else None
        pl.append(placement("dialogue", s["shot_id"], s["stem_file"], s["stem_at_s"], 0.0, 0.0,
                            s["stem_keep_s"] if s["lag_capped"] else None, None, fade_out, voice=True, shot_id=s["shot_id"]))
        meta = load_json(Path(s["stem_file"]).parent / "dialogue_meta.json") or {}
        lines = []
        for ln in meta.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            st = float(ln.get("start_s", s["anchor_s"]))
            lines.append({"start_s": round(s["stem_at_s"] + (st - s["anchor_s"]), 3),
                          "duration_s": float(ln.get("duration_s") or 0.0), "text": ln.get("text", "")})
        voices.append({"kind": "dialogue", "id": s["shot_id"], "shot_id": s["shot_id"],
                       "character_id": meta.get("character_id") or s.get("character_id"),
                       "text": meta.get("text", ""), "film_start_s": s["stem_at_s"], "film_end_s": s["stem_end_s"],
                       "lines": lines})

    # ---- narration (a voice; no extra duck)
    for n in tl.get("narration", []):
        pl.append(placement("narration", n["id"], n["file"], n["film_start_s"], NARRATION_DB, voice=True))
        voices.append({"kind": "narration", "id": n["id"], "shot_id": n["starts_on"], "character_id": "narrator",
                       "text": n.get("text", ""), "film_start_s": n["film_start_s"], "film_end_s": n["film_end_s"],
                       "lines": [{"start_s": n["film_start_s"], "duration_s": n["duration_s"], "text": n.get("text", "")}]})

    # ---- overlap per boundary (beds / sfx / native cross the cut)
    ov_in = {0: 0.0}
    ov_out = {}
    for i, (a, b) in enumerate(zip(segs, segs[1:])):
        ov = max(OV_MIN_S, b["audio_lead_s"], a["audio_lag_s"])
        ov_out[i], ov_in[i + 1] = ov, ov
    ov_out[len(segs) - 1] = 0.0

    def crossing(layer, pid, file, s, gain_db, src_offset=0.0, **extra):
        """Place a per-clip source that must cross the cuts: source time == clip time."""
        oi, oo = ov_in[s["idx"]], ov_out[s["idx"]]
        src_in = s["src_in_s"] - oi + src_offset
        at = s["film_start_s"] - oi
        if src_in < 0:                      # clip has no audio before frame 0: shift the window
            at -= src_in
            src_in = 0.0
        src_out = s["src_out_s"] + oo + src_offset
        fi = [s["film_start_s"] - oi, s["film_start_s"] + oi, "equal_power"] if oi > 0 else [at, at + 0.02, "linear"]
        fo = [s["film_end_s"] - oo, s["film_end_s"] + oo, "equal_power"] if oo > 0 else [s["film_end_s"] - 0.02, s["film_end_s"], "linear"]
        pl.append(placement(layer, pid, file, at, gain_db, src_in, src_out, fi, fo, shot_id=s["shot_id"], **extra))

    # ---- per-clip sfx
    if not opts.get("no_sfx"):
        for s in segs:
            f = run_dir / "clips" / s["shot_id"] / "sfx.wav"
            if f.is_file():
                crossing("sfx", f"sfx:{s['shot_id']}", f, s, 0.0)

    # ---- native H3 ambience (opt-in, only on clips that verifiably carry no voice)
    if opts.get("keep_native_ambience"):
        import speech_detect  # local module
        for s in segs:
            cdir = run_dir / "clips" / s["shot_id"]
            clip = cdir / "clip.mp4"
            meta = load_json(cdir / "dialogue_meta.json")
            if meta is not None and (meta.get("status") != "no_speech" or meta.get("muxed_into_clip")):
                continue
            if not clip.is_file():
                warnings.append(f"{s['shot_id']}: --keep-native-ambience but clips/{s['shot_id']}/clip.mp4 is missing")
                continue
            if not has_audio_stream(str(clip)):
                warnings.append(f"{s['shot_id']}: clip.mp4 has no audio stream — no native ambience")
                continue
            tmp = run_dir / "final" / "native" / f"{s['shot_id']}.wav"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            if not tmp.is_file() or tmp.stat().st_mtime < clip.stat().st_mtime:
                run_ff(["-i", clip, "-vn", "-ac", "2", "-ar", str(SR), "-c:a", "pcm_s16le", tmp], f"native audio {s['shot_id']}")
            det = speech_detect.detect(str(tmp))
            if det["speech"]:
                warnings.append(f"{s['shot_id']}: native H3 track contains speech ({det['speech_s']:.1f} s) — not used")
                continue
            crossing("native", f"native:{s['shot_id']}", tmp, s, NATIVE_BED_DB, source_layer="bed")

    # ---- music cues and beds (music/music_cues.resolved.json from music_render.py --validate)
    cues_out: list[dict] = []
    cues_path = run_dir / "music" / "music_cues.resolved.json"
    if not opts.get("no_music"):
        if not cues_path.is_file():
            raise InputError(f"missing {cues_path} — run pipeline/music_render.py --validate first "
                             f"(or pass --no-music to assemble without music)")
        mc = load_json(cues_path, required=True)
        if not isinstance(mc, dict):
            raise InputError(f"{cues_path}: must be an object with 'cues' and 'beds'")
        music_level = float(mc.get("music_level_db", DUCK["music"][0]))
        bed_level = float(mc.get("bed_level_db", 0.0))
        ref_lufs = float(mc.get("ref_lufs", MUSIC_REF_LUFS))
        start_of, end_of = {}, {}
        for s in segs:
            start_of.setdefault(s["shot_id"], s["film_start_s"])
            end_of[s["shot_id"]] = s["film_end_s"]

        def span_of(item: dict, what: str) -> tuple[float, float | None]:
            if item.get("film_start_s") is not None:
                st = float(item["film_start_s"])
            elif item.get("start_shot") in start_of:
                st = start_of[item["start_shot"]] + float(item.get("start_offset_s") or 0.0)
            elif item.get("scene_id") is not None:
                sc = next((x for x in tl["scenes"] if x["scene_id"] == item["scene_id"]), None)
                if sc is None:
                    raise InputError(f"{what}: scene {item['scene_id']} is not in the timeline")
                return sc["film_start_s"], sc["film_end_s"]
            else:
                raise InputError(f"{what}: needs film_start_s (resolved cue) or start_shot/scene_id")
            if item.get("film_end_s") is not None:
                en = float(item["film_end_s"])
            elif item.get("end_shot") in end_of:
                en = end_of[item["end_shot"]] + float(item.get("end_offset_s") or 0.0)
            else:
                en = None
            return st, en

        cues = []
        for i, c in enumerate(mc.get("cues") or []):
            cid = str(c.get("id") or f"M{i + 1:02d}")
            f = run_dir / "music" / "cues" / f"{cid}.wav"
            if not f.is_file():
                raise InputError(f"music cue {cid}: {f} missing (use --no-music to assemble without music)")
            st, en = span_of(c, f"music cue {cid}")
            lufs = c.get("lufs")
            if lufs is None:
                sc = load_json(f.with_suffix(".json"))
                lufs = sc.get("lufs") if isinstance(sc, dict) else None
            norm = (ref_lufs - float(lufs)) if isinstance(lufs, (int, float)) else 0.0
            if lufs is None:
                warnings.append(f"music cue {cid}: no lufs (cue entry or {f.with_suffix('.json').name}) — not loudness-matched")
            cues.append({"id": cid, "file": f, "st": st, "en": en, "gain": norm + float(c.get("level_db") or 0.0),
                         "tin": c.get("transition_in") or {"type": "fade", "s": 1.0},
                         "tout": c.get("transition_out") or {"type": "fade", "s": 1.0},
                         "xf": float(c.get("crossfade_s") or 2.0)})
        cues.sort(key=lambda c: c["st"])
        for i, c in enumerate(cues):
            nxt = cues[i + 1] if i + 1 < len(cues) else None
            if c["en"] is None:
                c["en"] = nxt["st"] if nxt else film_end
            if nxt and c["en"] > nxt["st"] + 1e-6 and not _is_xfade(c["tout"], nxt["tin"]):
                warnings.append(f"music cue {c['id']}: ends {c['en'] - nxt['st']:.2f} s inside {nxt['id']} — trimmed to {nxt['id']}'s start")
                c["en"] = nxt["st"]
        for i, c in enumerate(cues):
            prv = cues[i - 1] if i > 0 else None
            nxt = cues[i + 1] if i + 1 < len(cues) else None
            st, en = c["st"], c["en"]
            fi = _fade_window(c["tin"], st, "in", prv and _is_xfade(prv["tout"], c["tin"]) and (prv["xf"] or c["xf"]))
            fo = _fade_window(c["tout"], en, "out", nxt and _is_xfade(c["tout"], nxt["tin"]) and (c["xf"] or nxt["xf"]))
            at, stop = st, en
            if fi and fi[0] < at:
                at = fi[0]
            if fo and fo[1] > stop:
                stop = fo[1]
            pl.append(placement("music", c["id"], c["file"], at, music_level + c["gain"], 0.0, stop - at, fi, fo))
            cues_out.append({"id": c["id"], "film_start_s": round(at, 3), "film_end_s": round(stop, 3)})
        for i, b in enumerate(mc.get("beds") or []):
            bid = str(b.get("id") or f"B{i + 1:02d}")
            f = run_dir / "music" / "beds" / f"{bid}.wav"
            if not f.is_file():
                raise InputError(f"bed {bid}: {f} missing")
            st, en = span_of(b, f"bed {bid}")
            if en is None:
                raise InputError(f"bed {bid}: needs film_end_s")
            xf = float(b.get("crossfade_s") or BED_XFADE_S)
            first, last = st <= 1e-9, abs(en - film_end) < 1e-6
            h = xf / 2
            at = st - (0.0 if first else h)
            stop = en + (0.0 if last else h)
            fi = [st, st + xf, "linear"] if first else [at, at + xf, "equal_power"]
            fo = [stop - xf, stop, "linear"] if last else [stop - xf, stop, "equal_power"]
            pl.append(placement("bed", bid, f, at, bed_level + float(b.get("level_db") or 0.0), 0.0, stop - at, fi, fo,
                                tile=True, scene_id=b.get("scene_id")))
    return pl, voices, cues_out


def _is_xfade(tout: dict, tin: dict) -> bool:
    return str((tout or {}).get("type", "")).lower() == "crossfade" or str((tin or {}).get("type", "")).lower() == "crossfade"


def _fade_window(tr: dict, t: float, which: str, xf_s) -> list | None:
    ttype = str((tr or {}).get("type", "fade")).lower()
    if xf_s:                                # equal-power crossfade centred on the cue boundary
        h = float(xf_s) / 2
        return [t - h, t + h, "equal_power"]
    if ttype in ("hard", "none"):
        return None
    s = float((tr or {}).get("s", 1.0))
    if s <= 0:
        return None
    return [t, t + s, "linear"] if which == "in" else [t - s, t, "linear"]


# ----------------------------------------------------------------------------- mixing
def place(buf: np.ndarray, x: np.ndarray, at_s: float, gain_db: float = 0.0, sr: int = SR) -> tuple[int, int]:
    """Add x (n, 2) to buf at at_s with gain.  No truncation at segment ends — only at the buffer's
    own edges (negative start or past the film end).  Returns the [i0, i1) range written."""
    i = int(round(at_s * sr))
    g = db_to_lin(gain_db)
    if i < 0:
        x = x[-i:]
        i = 0
    n = min(len(x), len(buf) - i)
    if n <= 0:
        return i, i
    buf[i:i + n] += x[:n] * g
    return i, i + n


def voice_mask_from(x: np.ndarray, sr: int = SR, thresh_dbfs: float = MASK_THRESH_DBFS) -> np.ndarray:
    """1 per sample where the signal's own 10 ms RMS exceeds thresh_dbfs."""
    mono = x.mean(axis=1) if x.ndim == 2 else x
    hop = sr // CTRL_HZ
    n = len(mono) // hop
    if n == 0:
        return np.zeros(len(mono), np.float32)
    rms = np.sqrt(np.mean(mono[:n * hop].reshape(n, hop) ** 2, axis=1))
    on = (20 * np.log10(np.maximum(rms, 1e-9)) > thresh_dbfs).astype(np.float32)
    return np.pad(np.repeat(on, hop), (0, len(mono) - n * hop), constant_values=0.0)


def gain_curve(voice_mask: np.ndarray, base_db: float, duck_db: float, sr: int = SR,
               attack_s: float = ATTACK_S, release_s: float = RELEASE_S) -> np.ndarray:
    """Per-sample linear gain for a layer: base_db away from voices, duck_db under them.
    Control rate 100 Hz; the attack is a look-ahead ramp (fully ducked when the voice starts,
    reached over attack_s before it), the release a linear ramp of release_s after it ends."""
    hop = sr // CTRL_HZ
    n = int(math.ceil(len(voice_mask) / hop))
    padded = np.pad(voice_mask, (0, n * hop - len(voice_mask)), constant_values=0.0)
    lvl = padded.reshape(n, hop).max(axis=1)
    att = max(1, int(round(attack_s * CTRL_HZ)))
    rel = max(1, int(round(release_s * CTRL_HZ)))
    env = np.zeros(n, np.float32)
    # look-ahead attack: ramp up over `att` frames so env == 1 at the first voiced frame
    onsets = np.flatnonzero(np.diff(np.concatenate(([0.0], lvl))) > 0)
    for o in onsets:
        k = np.arange(max(0, o - att), o)
        env[k] = np.maximum(env[k], 1.0 - (o - k) / att)
    env = np.maximum(env, lvl)
    # linear release
    cur = 0.0
    step = 1.0 / rel
    for i in range(n):
        cur = max(env[i], cur - step) if env[i] < cur else env[i]
        env[i] = cur
    g_db = base_db + (duck_db - base_db) * env
    g = np.repeat(10 ** (g_db / 20.0), hop).astype(np.float32)
    return g[:len(voice_mask)]


def fade_gain(n_samples: int, i0: int, window, sr: int = SR, direction: str = "in") -> np.ndarray | None:
    """Per-sample gain (n_samples,) for a fade window given in film time; i0 = film sample of x[0]."""
    if not window:
        return None
    t0, t1, shape = window
    a, b = int(round(t0 * sr)) - i0, int(round(t1 * sr)) - i0
    if b <= a:
        return None
    frac = np.clip((np.arange(n_samples, dtype=np.float32) - a) / float(b - a), 0.0, 1.0)
    if shape == "equal_power":
        return (np.sin(frac * (math.pi / 2)) if direction == "in" else np.cos(frac * (math.pi / 2))).astype(np.float32)
    return (frac if direction == "in" else 1.0 - frac).astype(np.float32)


def render_mix(placements: list[dict], n_samples: int, loader=load_audio, opts: dict | None = None,
               sr: int = SR) -> dict:
    """Place every item, build the voice mask, duck, sum.  Returns buffers + stats."""
    opts = opts or {}
    bufs = {k: np.zeros((n_samples, 2), np.float32) for k in ("dialogue", "narration", "music", "bed", "sfx")}
    mask = np.zeros(n_samples, np.float32)
    cache: dict[str, np.ndarray] = {}
    items: list[dict] = []
    short: list[str] = []
    for p in placements:
        key = p["file"]
        if key not in cache:
            cache[key] = loader(p["file"], sr)
        x = cache[key]
        i_in = int(round(p["src_in_s"] * sr))
        i_out = len(x) if p["src_out_s"] is None else int(round(p["src_out_s"] * sr))
        if p.get("tile") and i_out > len(x) and len(x) > 0:
            reps = int(math.ceil(i_out / len(x)))
            x = np.tile(x, (reps, 1))
            short.append(f"{p['id']}: {Path(p['file']).name} looped {reps}x to cover {i_out / sr:.1f} s")
        elif p["src_out_s"] is not None and i_out > len(x):
            short.append(f"{p['id']}: {Path(p['file']).name} is {len(x) / sr:.2f} s, placement wanted {p['src_out_s']:.2f} s — ends early")
        x = x[i_in:i_out]
        if len(x) == 0:
            raise InputError(f"placement {p['id']}: {p['file']} has no audio in [{p['src_in_s']}, {p['src_out_s']}] s")
        x = x.copy()
        i0 = int(round(p["at_s"] * sr))
        gi = fade_gain(len(x), i0, p.get("fade_in"), sr, "in")
        go = fade_gain(len(x), i0, p.get("fade_out"), sr, "out")
        if gi is not None:
            x *= gi[:, None]
        if go is not None:
            x *= go[:, None]
        layer = "bed" if p["layer"] == "native" else p["layer"]
        a, b = place(bufs[layer], x, p["at_s"], p["gain_db"], sr)
        if p.get("voice"):
            m = voice_mask_from(x, sr)
            if i0 < 0:
                m = m[-i0:]
                i0 = 0
            n = min(len(m), n_samples - i0)
            if n > 0:
                mask[i0:i0 + n] = np.maximum(mask[i0:i0 + n], m[:n])
        items.append({"id": p["id"], "layer": p["layer"], "film_start_s": round(a / sr, 3), "film_end_s": round(b / sr, 3)})
    out = bufs["dialogue"] + bufs["narration"]
    ducking = {}
    for layer, (base, under) in DUCK.items():
        if layer == "sfx" and opts.get("sfx_mute_under_voice"):
            under = -120.0
        g = gain_curve(mask, base, under, sr)
        ducking[layer] = round(float(np.sum(g < db_to_lin(base) - 1e-6)) / sr, 3)
        out += bufs[layer] * g[:, None]
    peak = float(np.max(np.abs(out))) if n_samples else 0.0
    guard = min(1.0, PEAK_GUARD / peak) if peak > PEAK_GUARD else 1.0
    out *= guard
    return {"mix": out, "mask": mask, "bufs": bufs, "ducking_s": ducking, "peak": peak, "guard": guard,
            "voice_s": round(float(mask.sum()) / sr, 3), "items": items, "warnings": short}


def loudnorm_two_pass(raw: Path, dest: Path) -> dict:
    cmd1 = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(raw), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
            "-f", "null", "-"]
    r = subprocess.run(cmd1, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if r.returncode != 0 or not m:
        raise FfmpegError(f"loudnorm pass 1 failed: {r.stderr[-600:]}")
    p1 = json.loads(m.group(0))
    af = (f"loudnorm=I=-16:TP=-1.5:LRA=11:measured_I={p1['input_i']}:measured_TP={p1['input_tp']}:"
          f"measured_LRA={p1['input_lra']}:measured_thresh={p1['input_thresh']}:offset={p1['target_offset']}:"
          f"linear=true:print_format=json")
    r2 = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(raw), "-af", af, "-ar", str(SR), "-ac", "2",
                         "-c:a", "pcm_s24le", str(dest)], capture_output=True, text=True)
    m2 = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r2.stderr, re.S)
    if r2.returncode != 0 or not dest.is_file():
        raise FfmpegError(f"loudnorm pass 2 failed: {r2.stderr[-600:]}")
    return {"pass1": p1, "pass2": json.loads(m2.group(0)) if m2 else {}}


# ----------------------------------------------------------------------------- checks + main
def output_checks(movie: Path, tl: dict) -> tuple[dict, list[str]]:
    info = probe_streams(str(movie))
    vids = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    auds = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    fails = []
    checks = {"video_streams": len(vids), "audio_streams": len(auds)}
    if len(vids) != 1:
        fails.append(f"expected one video stream, found {len(vids)}")
    if len(auds) != 1:
        fails.append(f"expected one audio stream, found {len(auds)}")
    if vids:
        fps = probe_fps(str(movie))
        checks["fps"] = fps
        if abs(fps - tl["fps"]) > 0.01:
            fails.append(f"video is {fps} fps, timeline is {tl['fps']} fps")
        frames = probe_packets(str(movie))
        checks["frames"] = frames
        if frames != tl["film_frames"]:
            fails.append(f"movie has {frames} frames, timeline sums to {tl['film_frames']}")
        vdur = float(vids[0].get("duration") or info.get("format", {}).get("duration") or 0.0)
    else:
        vdur = 0.0
    if auds:
        checks["sample_rate"] = int(auds[0].get("sample_rate") or 0)
        checks["channels"] = int(auds[0].get("channels") or 0)
        if checks["sample_rate"] != SR:
            fails.append(f"audio is {checks['sample_rate']} Hz, expected {SR}")
        if checks["channels"] != 2:
            fails.append(f"audio has {checks['channels']} channels, expected 2")
        adur = float(auds[0].get("duration") or info.get("format", {}).get("duration") or 0.0)
        checks["audio_minus_video_s"] = round(adur - vdur, 4)
        if abs(adur - vdur) >= 0.05:
            fails.append(f"audio/video duration differ by {adur - vdur:+.3f} s")
    return checks, fails


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--timeline-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate-decisions", action="store_true")
    ap.add_argument("--encoder", choices=("libx264", "h264_nvenc"), default="libx264")
    ap.add_argument("--keep-native-ambience", action="store_true")
    ap.add_argument("--no-music", action="store_true")
    ap.add_argument("--no-sfx", action="store_true")
    ap.add_argument("--sfx-mute-under-voice", action="store_true")
    ap.add_argument("--force-segments", action="store_true")
    ap.add_argument("--force-mix", action="store_true")
    ap.add_argument("--master-mov", action="store_true")
    ap.add_argument("--narration-fit", action="store_true")
    ap.add_argument("--decisions", default=None, help="edit decisions file (default final/edit_decisions.json)")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir).resolve()
    final = run_dir / "final"
    opts = {"no_music": a.no_music, "no_sfx": a.no_sfx, "keep_native_ambience": a.keep_native_ambience,
            "sfx_mute_under_voice": a.sfx_mute_under_voice}
    alog: dict = {"status": "started", "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "run_dir": str(run_dir),
                  "options": dict(vars(a)), "inputs": {}, "segments": [], "layers": {}, "warnings": []}
    try:
        if not run_dir.is_dir():
            raise InputError(f"run dir not found: {run_dir}")
        if not (a.dry_run or a.validate_decisions) and not have_ffmpeg():
            raise InputError("ffmpeg/ffprobe not found on PATH")
        tl = build_timeline(run_dir, narration_fit=a.narration_fit and not (a.dry_run or a.validate_decisions),
                            decisions_path=Path(a.decisions) if a.decisions else None)
        placements, voices, cues_out = build_placements(tl, run_dir, opts, tl["warnings"])
        if a.validate_decisions:
            print(f"edit decisions valid: {len(tl['boundaries'])} boundaries, {len(tl['segments'])} segments, "
                  f"{tl['film_duration_s']:.3f} s at {tl['fps']} fps")
            for w in tl["warnings"]:
                print(f"  warning: {w}")
            return EXIT_OK
        if a.dry_run:
            print(dry_run_table(tl, cues_out))
            return EXIT_OK
        final.mkdir(parents=True, exist_ok=True)
        write_json(final / "timeline.json", tl)
        log(f"timeline: {len(tl['segments'])} segments, {tl['film_frames']} frames @ {tl['fps']} fps = {tl['film_duration_s']:.3f} s")
        if a.timeline_only:
            return EXIT_OK
        for p in (run_dir / "shot_list.json", run_dir / "audio" / "narration" / "narration_meta.json",
                  run_dir / "music" / "music_cues.resolved.json", run_dir / "assembly_fixes.json", final / "edit_decisions.json"):
            if p.is_file():
                alog["inputs"][str(p.relative_to(run_dir))] = file_sig(p)
        for s in tl["segments"]:
            alog["inputs"][f"clips/{s['shot_id']}/{Path(s['src']).name}"] = file_sig(Path(s["src"]))

        # ---- picture
        picture = build_picture(tl, final, a.encoder, a.force_segments, alog)

        # ---- mix
        n_samples = int(round(tl["film_frames"] * SR / tl["fps"]))
        key_src = {"placements": placements, "n": n_samples, "opts": opts,
                   "files": {p["file"]: file_sig(Path(p["file"])) for p in placements}}
        mix_key = sha1_of(key_src)
        cache_p = final / "mix_cache.json"
        prev = load_json(cache_p)
        mix_wav = final / "mix.wav"
        if (not a.force_mix and isinstance(prev, dict) and prev.get("key") == mix_key and mix_wav.is_file()
                and (final / "mix_raw.wav").is_file()):
            log("mix: cached (placements unchanged)")
            alog["mix"] = dict(prev, cached=True)
        else:
            res = render_mix(placements, n_samples, load_audio, opts)
            tl["warnings"].extend(res["warnings"])
            write_wav_f32(final / "mix_raw.wav", res["mix"])
            ln = loudnorm_two_pass(final / "mix_raw.wav", mix_wav)
            alog["mix"] = {"key": mix_key, "cached": False, "peak_before_guard": round(res["peak"], 4), "guard": round(res["guard"], 4),
                           "voice_s": res["voice_s"], "ducking_s": res["ducking_s"], "loudnorm": ln}
            write_json(cache_p, {"key": mix_key, "peak_before_guard": alog["mix"]["peak_before_guard"], "guard": alog["mix"]["guard"],
                                 "voice_s": res["voice_s"], "ducking_s": res["ducking_s"], "loudnorm": ln})
            log(f"mix: {len(placements)} placements, voice {res['voice_s']:.1f} s, peak {res['peak']:.3f}, "
                f"loudnorm in {ln['pass1'].get('input_i')} LUFS")
        alog["layers"] = {"dialogue": sum(p["layer"] == "dialogue" for p in placements),
                          "narration": sum(p["layer"] == "narration" for p in placements),
                          "cues": [p["id"] for p in placements if p["layer"] == "music"],
                          "beds": [p["id"] for p in placements if p["layer"] == "bed"],
                          "sfx": sum(p["layer"] == "sfx" for p in placements),
                          "native_ambience": sum(p["layer"] == "native" for p in placements)}
        alog["ducking_s"] = alog["mix"].get("ducking_s")
        alog["loudnorm"] = alog["mix"].get("loudnorm")
        write_json(final / "audio_placements.json", {"fps": tl["fps"], "sr": SR, "film_duration_s": tl["film_duration_s"],
                                                     "voices": voices, "placements": placements})

        # ---- mux
        movie = final / "movie_4k.mp4"
        run_ff(["-i", picture, "-i", mix_wav, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                "-shortest", "-movflags", "+faststart", movie], "final mux")
        if a.master_mov:
            master = final / "movie_4k_master.mov"
            run_ff(["-i", picture, "-i", mix_wav, "-map", "0:v", "-map", "1:a", "-c:v", "prores_ks", "-profile:v", "3",
                    "-c:a", "pcm_s24le", master], "ProRes master")
            alog["master_mov"] = str(master)
        checks, fails = output_checks(movie, tl)
        alog["ffprobe_checks"] = checks
        alog["timeline"] = {"fps": tl["fps"], "film_frames": tl["film_frames"], "film_duration_s": tl["film_duration_s"],
                            "segments": len(tl["segments"]), "boundaries": len(tl["boundaries"]),
                            "decided": sum(b["source"] == "edit_decisions" for b in tl["boundaries"])}
        alog["warnings"] = tl["warnings"]
        alog["movie"] = str(movie)
        if fails:
            alog["status"] = "failed_checks"
            alog["check_failures"] = fails
            write_json(final / "assembly_log.json", alog)
            for f in fails:
                log(f"CHECK FAILED: {f}")
            return EXIT_DECISION
        alog["status"] = "ok"
        alog["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        write_json(final / "assembly_log.json", alog)
        log(f"done: {movie} ({tl['film_duration_s']:.2f} s, {len(tl['segments'])} shots); {len(tl['warnings'])} warning(s)")
        return EXIT_OK
    except AssemblyError as exc:
        alog["status"] = "error"
        alog["error"] = str(exc)
        alog["error_code"] = exc.code
        if final.is_dir() and not (a.dry_run or a.validate_decisions):
            write_json(final / "assembly_log.json", alog)
        print(f"assemble: ERROR ({exc.code}) {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
