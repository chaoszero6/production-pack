#!/usr/bin/env python3
"""Build exact-duration dialogue.wav beds for every shot that has dialogue.

Usage:
  python3 pipeline/build_dialogue_stems.py [--shot S02_005] [--all] [--force]

Reads shot_list.json + audio/voices/voice_config.json, synthesises via CosyVoice
:50000, time-fits speech into [start_time, end_time], and writes an exact
bed_duration wav + dialogue_meta.json into clips/<shot>/.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime

RUN_DIR = os.environ.get(
    "RUN_DIR", "/root/production_pack/output/run_20260921_231424"
)
WORK = os.path.join(RUN_DIR, "audio", "voices", "_work")
TTS_URL = os.environ.get("COSYVOICE_URL", "http://127.0.0.1:50000/tts")
SPEEDS = (1.0, 1.2, 1.4, 1.5, 1.6, 1.8, 2.0, 0.9, 1.1, 1.3, 2.2, 2.5)


def probe_dur(path: str) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            path,
        ]
    )
    return float(out.decode().strip())


def tts(text: str, ref: str, prompt_text: str, speed: float, out: str) -> None:
    body = json.dumps(
        {
            "text": text,
            "reference_audio_path": ref,
            "prompt_text": prompt_text,
            "speed": speed,
        }
    ).encode()
    req = urllib.request.Request(
        TTS_URL, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = resp.read()
    if len(data) < 100:
        raise RuntimeError(f"TTS returned {len(data)} bytes")
    with open(out, "wb") as fh:
        fh.write(data)


def build(shot: dict, chars: dict, force: bool = False) -> bool:
    sid = shot["shot_id"]
    dialogue_lines = (shot.get("audio") or {}).get("dialogue") or []
    if not dialogue_lines:
        return True

    clip = os.path.join(RUN_DIR, "clips", sid)
    os.makedirs(clip, exist_ok=True)
    wav_path = os.path.join(clip, "dialogue.wav")
    meta_path = os.path.join(clip, "dialogue_meta.json")
    bed = float(shot["duration_seconds"])

    if not force and os.path.isfile(wav_path) and os.path.isfile(meta_path):
        try:
            if abs(probe_dur(wav_path) - bed) < 0.06:
                meta = json.load(open(meta_path))
                if not meta.get("failed") and meta.get("character_id"):
                    print(f"{sid}: skip (valid stem)")
                    return True
        except Exception:
            pass

    dlg0 = dialogue_lines[0]
    cid = dlg0["character_id"]
    if cid not in chars:
        print(f"{sid}: unknown character {cid}")
        return False
    ch = chars[cid]
    t0 = float(dlg0.get("start_time", 0.5))
    t1 = float(max(float(d.get("end_time", bed)) for d in dialogue_lines))
    line = " ".join(d["line"] for d in dialogue_lines)
    ref = ch["reference_audio"]
    pt = ch["prompt_text"]
    window = max(t1 - t0, 0.5)

    os.makedirs(WORK, exist_ok=True)
    cands: list[tuple[float, str, float]] = []
    for spd in SPEEDS:
        raw = os.path.join(WORK, f"{sid.lower()}_raw_{spd}.wav")
        if not (os.path.isfile(raw) and os.path.getsize(raw) > 100):
            try:
                tts(line, ref, pt, spd, raw)
            except Exception as exc:
                print(f"  tts fail speed={spd}: {exc}")
                continue
        try:
            d = probe_dur(raw)
        except Exception:
            continue
        cands.append((spd, raw, d))
    if not cands:
        print(f"{sid}: no TTS candidates")
        return False

    fit = [c for c in cands if 0.65 * window <= c[2] <= 1.35 * window] or cands
    spd, raw, sdur = min(fit, key=lambda c: abs(c[2] - window))

    filters: list[str] = []
    if sdur > window + 0.02:
        ratio = min(sdur / window, 3.0)
        while ratio > 2.0:
            filters.append("atempo=2.0")
            ratio /= 2.0
        if abs(ratio - 1.0) > 0.01:
            filters.append(f"atempo={ratio:.6f}")
    elif sdur < window * 0.45:
        ratio = min(max(window * 0.7 / sdur, 1.0), 2.0)
        if abs(ratio - 1.0) > 0.02:
            filters.append(f"atempo={ratio:.6f}")

    total_r = 1.0
    for f in filters:
        if f.startswith("atempo="):
            total_r *= float(f.split("=", 1)[1])
    speech = sdur / total_r if total_r else sdur

    pre = (",".join(filters) + ",") if filters else ""
    # adelay head → exact bed via -t (atrim alone can undershoot after apad)
    af = (
        f"{pre}adelay={int(round(t0 * 1000))}:all=1,"
        f"apad=whole_dur={bed},atrim=0:{bed},asetpts=N/SR/TB"
    )
    tmp = os.path.join(WORK, f"{sid.lower()}_build.wav")
    subprocess.check_call(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            raw,
            "-af",
            af,
            "-t",
            str(bed),
            "-ar",
            "44100",
            "-ac",
            "1",
            tmp,
        ]
    )
    bdur = probe_dur(tmp)
    if abs(bdur - bed) > 0.06:
        # second pass: hard pad/trim to bed (tmp is already a .wav path)
        fixed = os.path.join(WORK, f"{sid.lower()}_build_fixed.wav")
        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                tmp,
                "-af",
                "apad",
                "-t",
                str(bed),
                "-ar",
                "44100",
                "-ac",
                "1",
                fixed,
            ]
        )
        bdur = probe_dur(fixed)
        if abs(bdur - bed) > 0.06:
            print(f"{sid}: BED FAIL {bdur:.3f} != {bed} (af={af})")
            return False
        os.replace(fixed, tmp)

    os.replace(tmp, wav_path)
    meta = {
        "shot_id": sid,
        "character_id": cid,
        "character": ch["character"],
        "engine": "cosyvoice3",
        "line": line,
        "emotion": dlg0.get("emotion"),
        "start_time": t0,
        "end_time": t1,
        "bed_duration_s": bed,
        "speech_duration_s": round(speech, 3),
        "sample_rate": 44100,
        "channels": 1,
        "reference_audio": ref,
        "prompt_text": pt,
        "speed": spd,
        "source_raw": raw,
        "output": wav_path,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": "Pre-synthesised via CosyVoice :50000; exact bed via -t.",
    }
    with open(meta_path, "w") as fh:
        json.dump(meta, fh, indent=2)
        fh.write("\n")
    print(
        f"OK {sid} bed={bdur:.3f} speech={speech:.2f}s speed={spd} "
        f"char={ch['character']} line={line[:40]!r}"
    )
    return True


def needs_stem(shot: dict) -> bool:
    dialogue = (shot.get("audio") or {}).get("dialogue")
    if not dialogue:
        return False
    sid = shot["shot_id"]
    verd = None
    vpath = os.path.join(RUN_DIR, "clips", sid, "qa_verdict.json")
    if os.path.isfile(vpath):
        try:
            verd = json.load(open(vpath)).get("verdict")
        except Exception:
            pass
    if verd == "PASS" and os.path.isfile(
        os.path.join(RUN_DIR, "clips", sid, "clip_4k60.mp4")
    ):
        return False  # already shipped; stem not required
    wav = os.path.join(RUN_DIR, "clips", sid, "dialogue.wav")
    meta = os.path.join(RUN_DIR, "clips", sid, "dialogue_meta.json")
    bed = float(shot["duration_seconds"])
    if os.path.isfile(wav) and os.path.isfile(meta):
        try:
            if abs(probe_dur(wav) - bed) < 0.06:
                m = json.load(open(meta))
                if m.get("character_id") and not m.get("failed"):
                    return False
        except Exception:
            return True
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    shots = json.load(open(os.path.join(RUN_DIR, "shot_list.json")))["shots"]
    vc = json.load(open(os.path.join(RUN_DIR, "audio", "voices", "voice_config.json")))
    chars = {c["id"]: c for c in vc["characters"]}

    if args.shot:
        ids = set(args.shot)
        todo = [s for s in shots if s["shot_id"] in ids]
    else:
        todo = [s for s in shots if needs_stem(s)]
    if args.limit:
        todo = todo[: args.limit]

    if not todo:
        print("nothing to do")
        return 0

    print(f"building {len(todo)} stems: {[s['shot_id'] for s in todo]}")
    ok = fail = 0
    for shot in todo:
        try:
            if build(shot, chars, force=args.force):
                ok += 1
            else:
                fail += 1
        except Exception as exc:
            print(f"ERR {shot['shot_id']}: {exc}")
            fail += 1
    print(f"done ok={ok} fail={fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
