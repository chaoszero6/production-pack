#!/usr/bin/env python3
"""subtitles.py --run-dir D [--embed] [--burn] — SRT + ASS from the film's audio placements.

Reads final/audio_placements.json (written by assemble.py; every voice carries its film-time
lines) so the cues are timed to the audio that is actually in the mix — not to shot-list
estimates.  Dialogue cues come from the stem's per-line timings; a narration block is chunked
into pieces of <= 14 words (earlier at sentence punctuation after 6 words) whose durations are
proportional to their word counts.  Each cue starts 0.1 s early, lasts 1.5–7 s and keeps 0.2 s
to the next cue.

Writes final/movie.srt and final/movie.ass (PlayRes 3840x2160; styles Dialogue = Arial 96 white
outline 4, Narration = Georgia 96 italic light-yellow).  Dialogue text is "[CHARACTER]: text",
narration "(Narrator) text" in italics.
  --embed   mux the SRT as mov_text into final/movie_4k.mp4 (temp file + os.replace)
  --burn    re-encode final/movie_4k.mp4 with the ASS burnt in -> final/movie_4k_subs.mp4
Exit 0 ok, 2 missing input, 3 ffmpeg failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

LEAD_S = 0.1
MIN_S = 1.5
MAX_S = 7.0
GAP_S = 0.2
MAX_WORDS = 14

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 3840
PlayResY: 2160
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Dialogue,Arial,96,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,4,2,2,120,120,110,1
Style: Narration,Georgia,96,&H00C8F0FF,&H000000FF,&H00000000,&H80000000,0,1,0,0,100,100,0,0,1,4,2,2,120,120,110,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def srt_time(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def chunk_words(text: str, max_words: int = MAX_WORDS) -> list[str]:
    pieces, cur = [], []
    for w in str(text).split():
        cur.append(w)
        if len(cur) >= max_words or (len(cur) >= 6 and re.search(r"[.!?,;:]$", w)):
            pieces.append(" ".join(cur))
            cur = []
    if cur:
        pieces.append(" ".join(cur))
    return pieces


def display_name(character_id: str | None, names: dict) -> str:
    cid = str(character_id or "").strip()
    if cid in names:
        return names[cid]
    return cid.replace("_", " ").title() if cid else "Voice"


def character_names(run_dir: Path) -> dict:
    """character_id -> display name from the run's story/bible/voice config when available."""
    names: dict = {}
    for p in (run_dir / "story.json", run_dir / "bible.json", run_dir / "audio" / "voices" / "voice_config.json"):
        if not p.is_file():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        chars = d.get("characters") if isinstance(d, dict) else None
        if isinstance(chars, dict):
            for k, v in chars.items():
                if isinstance(v, dict) and v.get("name"):
                    names.setdefault(k, str(v["name"]))
        elif isinstance(chars, list):
            for c in chars:
                if isinstance(c, dict) and (c.get("character_id") or c.get("id")) and c.get("name"):
                    names.setdefault(c.get("character_id") or c.get("id"), str(c["name"]))
        if isinstance(d, dict) and chars is None:            # voice_config: {role: {name?...}}
            for k, v in d.items():
                if isinstance(v, dict) and v.get("name"):
                    names.setdefault(k, str(v["name"]))
    return names


def build_cues(placements: dict, names: dict) -> list[dict]:
    """[{start, end, kind, who, text}] in film time, clamped and de-overlapped."""
    cues = []
    for v in placements.get("voices", []):
        kind = v.get("kind")
        if kind == "dialogue":
            who = display_name(v.get("character_id"), names)
            lines = v.get("lines") or [{"start_s": v["film_start_s"], "duration_s": v["film_end_s"] - v["film_start_s"],
                                        "text": v.get("text", "")}]
            for ln in lines:
                if not str(ln.get("text", "")).strip():
                    continue
                st = float(ln["start_s"])
                cues.append({"start": st, "end": st + float(ln.get("duration_s") or 0.0), "kind": "dialogue",
                             "who": who, "text": str(ln["text"]).strip()})
        elif kind == "narration":
            pieces = chunk_words(v.get("text", ""))
            if not pieces:
                continue
            total_w = sum(len(p.split()) for p in pieces)
            dur = float(v["film_end_s"]) - float(v["film_start_s"])
            t = float(v["film_start_s"])
            for p in pieces:
                d = dur * len(p.split()) / total_w
                cues.append({"start": t, "end": t + d - 0.05, "kind": "narration", "who": "Narrator", "text": p})
                t += d
    cues.sort(key=lambda c: c["start"])
    out = []
    for c in cues:
        st = max(0.0, c["start"] - LEAD_S)
        en = max(st + MIN_S, min(st + MAX_S, c["end"]))
        if out and st < out[-1]["end"] + GAP_S:
            out[-1]["end"] = max(out[-1]["start"] + 0.5, st - GAP_S)
        out.append(dict(c, start=round(st, 3), end=round(en, 3)))
    return out


def write_srt(cues: list[dict], path: Path) -> None:
    body = []
    for i, c in enumerate(cues, 1):
        text = f"[{c['who'].upper()}]: {c['text']}" if c["kind"] == "dialogue" else f"<i>(Narrator) {c['text']}</i>"
        body.append(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{text}\n")
    path.write_text("\n".join(body) + ("\n" if body else ""), encoding="utf-8")


def write_ass(cues: list[dict], path: Path) -> None:
    ev = []
    for c in cues:
        txt = c["text"].replace("\n", "\\N").replace("{", "(").replace("}", ")")
        if c["kind"] == "dialogue":
            ev.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Dialogue,{c['who']},0,0,0,,[{c['who'].upper()}]: {txt}")
        else:
            ev.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Narration,Narrator,0,0,0,,{{\\i1}}(Narrator) {txt}{{\\i0}}")
    path.write_text(ASS_HEADER + "\n".join(ev) + ("\n" if ev else ""), encoding="utf-8")


def ff(args: list, what: str) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *map(str, args)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({what}): {r.stderr.strip()[-600:]}")


def embed(movie: Path, srt: Path) -> None:
    fd, tmp = tempfile.mkstemp(dir=movie.parent, prefix=".sub_", suffix=".mp4")
    os.close(fd)
    try:
        ff(["-i", movie, "-i", srt, "-map", "0", "-map", "-0:s", "-map", "1:0", "-c", "copy", "-c:s", "mov_text",
            "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", tmp], "embed mov_text")
        os.replace(tmp, movie)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ass_filter_path(p: Path) -> str:
    s = p.as_posix()
    if re.match(r"^[A-Za-z]:/", s):            # Windows drive letter must be escaped for the filter parser
        s = s[0] + "\\:" + s[2:]
    return s.replace("'", r"\'")


def burn(movie: Path, ass: Path, dest: Path) -> None:
    ff(["-i", movie, "-vf", f"ass='{ass_filter_path(ass)}'", "-c:v", "libx264", "-preset", "slow", "-crf", "16",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", dest], "burn ASS")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--embed", action="store_true")
    ap.add_argument("--burn", action="store_true")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir)
    final = run_dir / "final"
    src = final / "audio_placements.json"
    if not src.is_file():
        print(f"subtitles: missing {src} — run assemble.py first", file=sys.stderr)
        return 2
    placements = json.loads(src.read_text(encoding="utf-8"))
    cues = build_cues(placements, character_names(run_dir))
    srt, ass = final / "movie.srt", final / "movie.ass"
    write_srt(cues, srt)
    write_ass(cues, ass)
    n_d = sum(c["kind"] == "dialogue" for c in cues)
    print(f"subtitles: {len(cues)} cues ({n_d} dialogue, {len(cues) - n_d} narration) -> {srt.name}, {ass.name}")
    movie = final / "movie_4k.mp4"
    try:
        if a.embed or a.burn:
            if not movie.is_file():
                print(f"subtitles: missing {movie}", file=sys.stderr)
                return 2
        if a.embed:
            embed(movie, srt)
            print(f"subtitles: mov_text track embedded in {movie.name}")
        if a.burn:
            dest = final / "movie_4k_subs.mp4"
            burn(movie, ass, dest)
            print(f"subtitles: burnt-in copy -> {dest.name}")
    except RuntimeError as exc:
        print(f"subtitles: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
