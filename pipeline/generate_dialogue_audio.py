#!/usr/bin/env python3
"""Generate dialogue audio directly via Orpheus TTS API.

Bypasses the dsh audio-producer agent to avoid the VRAM conflict where
NInfer-US (28GB) + Orpheus (5GB) > 32GB RTX 5090. This script:
  1. Stops NInfer-US to free VRAM
  2. Starts Orpheus backend + TTS
  3. Generates dialogue wav via API
  4. Stops Orpheus
  5. (NInfer-US restarted later by pipeline when needed)

Usage: generate_dialogue_audio.py <shot_list.json> <shot_id> <clip_dir>
"""
import json
import os
import subprocess
import sys
import time

try:
    import requests
except ImportError:
    requests = None

ORPHEUS_URL = "http://127.0.0.1:9883/v1/audio/speech"
# Readiness probe must target a route that actually exists on the 9883 TTS
# FastAPI layer. `/v1/models` lives on the 9884 backend, NOT on 9883 — polling
# it there always 404s, so start_orpheus() used to time out and report failure
# even when Orpheus was up (caused repeated "Orpheus TTS failed to start" and
# "Direct audio generation failed" for every dialogue clip).
ORPHEUS_READY_URL = "http://127.0.0.1:9883/v1/audio/voices"

CHARACTER_VOICE_MAP = {
    "tobias": "jess",
    "nana_cog": "leah",
    "nana": "leah",
    "mayor_bangle": "dan",
    "mayor": "dan",
    "tick": "zoe",
    "clockwork_moth": "zoe",
    "the_clockwork_moth": "zoe",
}

DEFAULT_VOICE = "tara"

PARALINGUISTIC_MAP = {
    "warm": "",
    "tender": "",
    "excited": "<gasp> ",
    "surprised": "<gasp> ",
    "worried": "<sigh> ",
    "sad": "<sniffle> ",
    "determined": "",
    "playful": "<chuckle> ",
    "amused": "<chuckle> ",
    "nervous": "",
    "gentle": "",
    "stern": "",
    "joyful": "<laugh> ",
    "neutral": "",
}


def log(msg):
    print(f"[audio] {msg}", flush=True)


def stop_ninfer():
    log("Stopping NInfer to free VRAM...")
    subprocess.run(["systemctl", "stop", "ninfer-us.service"],
                    capture_output=True, timeout=30)
    subprocess.run(["systemctl", "stop", "ninfer.service"],
                    capture_output=True, timeout=30)
    time.sleep(2)


def start_ninfer():
    """Restart NInfer so the next agent (QA, etc.) has its LLM available."""
    log("Starting NInfer...")
    # Try ninfer first (preferred), then ninfer-us as fallback
    for svc, port in [("ninfer.service", 8080), ("ninfer-us.service", 8081)]:
        subprocess.run(["systemctl", "start", svc],
                       capture_output=True, timeout=60)
        for _ in range(120):
            try:
                r = requests.get(f"http://127.0.0.1:{port}/health", timeout=3)
                if r.status_code == 200:
                    log(f"NInfer ready on :{port} ({svc})")
                    return True
            except Exception:
                pass
            time.sleep(1)
        # Didn't come up — stop it and try the next one
        subprocess.run(["systemctl", "stop", svc], capture_output=True, timeout=30)
    log("WARNING: No NInfer service started in time")
    return False


def start_orpheus():
    log("Starting Orpheus TTS...")
    subprocess.run(["systemctl", "start", "orpheus-backend.service"],
                    capture_output=True, timeout=30)
    time.sleep(3)
    subprocess.run(["systemctl", "start", "orpheus-tts.service"],
                    capture_output=True, timeout=30)
    for _ in range(60):
        try:
            r = requests.get(ORPHEUS_READY_URL, timeout=3)
            if r.status_code == 200:
                log("Orpheus TTS ready")
                return True
        except Exception:
            pass
        time.sleep(1)
    log("ERROR: Orpheus TTS failed to start")
    return False


def stop_orpheus():
    log("Stopping Orpheus TTS...")
    subprocess.run(["systemctl", "stop", "orpheus-tts.service"],
                    capture_output=True, timeout=30)
    subprocess.run(["systemctl", "stop", "orpheus-backend.service"],
                    capture_output=True, timeout=30)
    time.sleep(2)


def get_voice(character_id):
    cid = (character_id or "").lower().replace(" ", "_").replace("-", "_")
    return CHARACTER_VOICE_MAP.get(cid, DEFAULT_VOICE)


def add_paralinguistic(text, emotion):
    prefix = PARALINGUISTIC_MAP.get((emotion or "").lower(), "")
    return prefix + text if prefix else text


def generate_audio(text, voice, output_path):
    if not requests:
        log("ERROR: requests module not available")
        return False
    try:
        r = requests.post(
            ORPHEUS_URL,
            json={
                "model": "orpheus",
                "input": text,
                "voice": voice,
                "response_format": "wav",
            },
            timeout=120,
        )
        if r.status_code == 200:
            with open(output_path, "wb") as f:
                f.write(r.content)
            size_kb = len(r.content) / 1024
            log(f"  Generated {size_kb:.0f}KB audio -> {output_path}")
            return True
        else:
            log(f"  ERROR: Orpheus returned {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        log(f"  ERROR: {e}")
        return False


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)

    shot_list_path = sys.argv[1]
    shot_id = sys.argv[2]
    clip_dir = sys.argv[3]

    with open(shot_list_path) as f:
        data = json.load(f)

    shot = next((s for s in data["shots"] if s["shot_id"] == shot_id), None)
    if not shot:
        sys.exit(f"Shot {shot_id} not found in shot list")

    dialogues = shot.get("audio", {}).get("dialogue", [])
    if not dialogues:
        log(f"No dialogue for {shot_id}")
        sys.exit(0)

    os.makedirs(clip_dir, exist_ok=True)
    wav_path = os.path.join(clip_dir, "dialogue.wav")
    meta_path = os.path.join(clip_dir, "dialogue_meta.json")

    emotional_beat = shot.get("emotional_beat", "neutral")
    log(f"Shot {shot_id}: {len(dialogues)} dialogue line(s), beat={emotional_beat}")

    # VRAM dance: stop NInfer, start Orpheus
    stop_ninfer()
    if not start_orpheus():
        log("Orpheus failed — writing failed meta")
        json.dump({"failed": True, "reason": "orpheus_startup_failed"},
                  open(meta_path, "w"), indent=2)
        sys.exit(1)

    # Generate audio for each dialogue line
    success = True
    meta = {
        "shot_id": shot_id,
        "engine_used": "orpheus",
        "dialogues": [],
    }

    if len(dialogues) == 1:
        d = dialogues[0]
        char_id = d.get("character_id", "unknown")
        voice = get_voice(char_id)
        line = d.get("line", "")
        emotion = d.get("emotion", emotional_beat)
        text = add_paralinguistic(line, emotion)

        log(f"  {char_id} ({voice}): {text[:80]}...")
        if generate_audio(text, voice, wav_path):
            meta["dialogues"].append({
                "character_id": char_id,
                "voice": voice,
                "emotion": emotion,
                "line": line,
            })
        else:
            success = False
    else:
        # Multiple lines — generate each, then concatenate with ffmpeg
        parts = []
        for i, d in enumerate(dialogues):
            char_id = d.get("character_id", "unknown")
            voice = get_voice(char_id)
            line = d.get("line", "")
            emotion = d.get("emotion", emotional_beat)
            text = add_paralinguistic(line, emotion)
            part_path = os.path.join(clip_dir, f"dialogue_part_{i}.wav")

            log(f"  [{i}] {char_id} ({voice}): {text[:80]}...")
            if generate_audio(text, voice, part_path):
                parts.append(part_path)
                meta["dialogues"].append({
                    "character_id": char_id,
                    "voice": voice,
                    "emotion": emotion,
                    "line": line,
                })
            else:
                success = False

        if parts:
            if len(parts) == 1:
                os.rename(parts[0], wav_path)
            else:
                concat_list = os.path.join(clip_dir, "concat_list.txt")
                with open(concat_list, "w") as f:
                    for p in parts:
                        f.write(f"file '{p}'\n")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                     "-i", concat_list, "-c", "copy", wav_path],
                    capture_output=True, timeout=30,
                )
                for p in parts:
                    os.remove(p)
                os.remove(concat_list)

    # Stop Orpheus to free VRAM for video generation
    stop_orpheus()

    # Restart NInfer so the next agent (QA, etc.) has its LLM available
    start_ninfer()

    meta["status"] = "completed" if success else "failed"
    meta["failed"] = not success
    json.dump(meta, open(meta_path, "w"), indent=2)

    if success and os.path.exists(wav_path):
        size_kb = os.path.getsize(wav_path) / 1024
        log(f"Done: {wav_path} ({size_kb:.0f}KB)")
    else:
        log("FAILED to generate dialogue audio")
        sys.exit(1)


if __name__ == "__main__":
    main()
