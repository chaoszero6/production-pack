#!/usr/bin/env python3
"""speech_detect.py <wav> [--json] — is there speech in this audio?

Used by assemble.py --keep-native-ambience to make sure a native H3 track that is about to be
used as an ambience bed carries no (hallucinated) voice.  Prints {"speech": bool, "speech_s": s}
and exits 0; exit 2 when the file cannot be read.

Detector: silero-vad via torch when both are importable (`torch.hub` cache or the `silero_vad`
package), otherwise a dependency-light heuristic — the 300–3000 Hz band energy envelope at
100 Hz, whose 2–8 Hz modulation (the syllable rate) is compared with the rest of the modulation
spectrum.  A 100 ms frame counts as speech when the band is above -45 dBFS and the syllabic
modulation share over a 1 s window exceeds 0.35.  The heuristic is tuned to err toward
"speech" (a false positive only costs one ambience bed; a false negative puts a second voice
under the real line).
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

SR = 16000
MIN_SPEECH_S = 0.3


def _read_wav_native(path: Path) -> tuple[np.ndarray, int]:
    raw = path.read_bytes()
    if raw[:4] != b"RIFF":
        raise ValueError("not a RIFF file")
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
        raise ValueError("missing fmt/data chunk")
    tag, ch, sr, _, _, bits = fmt
    if tag == 3:
        x = np.frombuffer(data, dtype="<f4").astype(np.float32)
    elif bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v >= 1 << 23, v - (1 << 24), v).astype(np.float32) / float(1 << 23)
    elif bits == 32:
        x = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV ({tag}/{bits} bit)")
    return x.reshape(-1, ch).mean(axis=1), sr


def _resample(x: np.ndarray, sr: int, target: int) -> np.ndarray:
    if sr == target:
        return x
    n = int(round(len(x) * target / sr))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def load_mono(path: str, sr: int = SR) -> np.ndarray:
    """Mono float32 at sr — ffmpeg when present (any container), else native WAV + linear resample."""
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                           capture_output=True)
        if r.returncode == 0 and r.stdout:
            return np.frombuffer(r.stdout, dtype=np.float32).copy()
    x, fs = _read_wav_native(Path(path))
    return _resample(x, fs, sr)


# ----------------------------------------------------------------------------- silero
def _silero(x: np.ndarray, sr: int) -> dict | None:
    try:
        import torch  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    model = get_ts = None
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad  # type: ignore
        model, get_ts = load_silero_vad(), get_speech_timestamps
    except Exception:  # noqa: BLE001
        try:
            model, utils = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True, verbose=False)
            get_ts = utils[0]
        except Exception:  # noqa: BLE001
            return None
    try:
        ts = get_ts(torch.from_numpy(x.astype(np.float32)), model, sampling_rate=sr)
        speech_s = sum((t["end"] - t["start"]) for t in ts) / sr
        return {"speech": speech_s >= MIN_SPEECH_S, "speech_s": round(float(speech_s), 3), "method": "silero-vad"}
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------- heuristic
def _heuristic(x: np.ndarray, sr: int) -> dict:
    hop = sr // 100                                      # 10 ms envelope frames
    win = hop * 2
    n = (len(x) - win) // hop + 1 if len(x) >= win else 0
    if n <= 10:
        return {"speech": False, "speech_s": 0.0, "method": "heuristic"}
    frames = np.lib.stride_tricks.as_strided(x, shape=(n, win), strides=(x.strides[0] * hop, x.strides[0])) * np.hanning(win)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    band = spec[:, (freqs >= 300) & (freqs <= 3000)].sum(axis=1)
    total = spec.sum(axis=1) + 1e-12
    env_db = 10 * np.log10(band / win + 1e-12)             # ~dBFS of the band
    ratio = band / total                                  # share of energy in the speech band
    # modulation spectrum of the band envelope over 1 s windows (100 frames)
    W = 100
    env = np.sqrt(band)
    speech_frames = np.zeros(n, bool)
    for c in range(0, n - W + 1, W // 4):
        seg = env[c:c + W]
        seg = seg - seg.mean()
        if np.max(np.abs(seg)) < 1e-9:
            continue
        ms = np.abs(np.fft.rfft(seg * np.hanning(W))) ** 2
        mf = np.fft.rfftfreq(W, 1.0 / 100)
        syl = ms[(mf >= 2) & (mf <= 8)].sum()
        rest = ms[(mf >= 0.5)].sum() + 1e-12
        if syl / rest > 0.35:
            speech_frames[c:c + W] |= (env_db[c:c + W] > -45) & (ratio[c:c + W] > 0.4)
    speech_s = float(speech_frames.sum()) / 100.0
    return {"speech": speech_s >= MIN_SPEECH_S, "speech_s": round(speech_s, 3), "method": "heuristic"}


def detect(path: str) -> dict:
    x = load_mono(path, SR)
    res = _silero(x, SR)
    if res is None:
        res = _heuristic(x, SR)
    res["duration_s"] = round(len(x) / SR, 3)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wav")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        res = detect(a.wav)
    except Exception as exc:  # noqa: BLE001
        print(f"speech_detect: cannot read {a.wav}: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(res))
    else:
        print(f"{'SPEECH' if res['speech'] else 'no speech'}: {res['speech_s']:.2f} s of {res['duration_s']:.2f} s ({res['method']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
