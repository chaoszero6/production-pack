#!/usr/bin/env python3
"""mux_stem.py <clip_dir> — give clip.mp4 its dialogue stem as the audio track.

PREVIEW / QA ONLY — assembly never reads this track. pipeline/assemble.py takes picture
only from the clips and places clips/<SID>/dialogue.wav on the film timeline itself.

The i2va graph writes video only (the native H3 track is never used), so a clip previewed
straight from clips/<SID>/clip.mp4 was silent even though the stem drove the mouth. This muxes
dialogue.wav at dialogue_meta.json's anchor_s (padded with silence before and after to the
clip length) so previews and QA hear the same approved audio at the same offset. The video
stream is copied untouched; the video-only original is kept as clip_video_only.mp4.
Idempotent: a clip that already carries the stem is left alone.
Exit 0 = muxed or nothing to do, 1 = ffmpeg failed.
"""
import json
import os
import subprocess
import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    clip_dir = sys.argv[1].rstrip("/")
    clip = os.path.join(clip_dir, "clip.mp4")
    stem = os.path.join(clip_dir, "dialogue.wav")
    meta_p = os.path.join(clip_dir, "dialogue_meta.json")
    if not (os.path.isfile(clip) and os.path.isfile(stem)):
        return 0
    try:
        meta = json.load(open(meta_p))
    except Exception:
        meta = {}
    if meta.get("status") not in (None, "ok"):
        return 0
    if meta.get("muxed_into_clip"):
        return 0
    anchor = float(meta.get("anchor_s") or 0.0)
    delay_ms = int(round(anchor * 1000))
    video_only = os.path.join(clip_dir, "clip_video_only.mp4")
    tmp = os.path.join(clip_dir, ".clip_mux.mp4")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", clip, "-i", stem,
           "-filter_complex", f"[1:a]aresample=48000,aformat=channel_layouts=stereo,adelay={delay_ms}|{delay_ms},apad[a]",
           "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(tmp):
        print(f"[mux] ffmpeg failed: {r.stderr[-300:]}")
        return 1
    if not os.path.exists(video_only):
        os.replace(clip, video_only)
    os.replace(tmp, clip)
    meta["muxed_into_clip"] = True
    meta["mux_delay_ms"] = delay_ms
    json.dump(meta, open(meta_p, "w"), indent=2)
    print(f"[mux] {os.path.basename(clip_dir)}: dialogue.wav muxed at {anchor:.2f}s (video-only copy: clip_video_only.mp4)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
