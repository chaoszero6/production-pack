#!/usr/bin/env python3
"""Kokoro TTS HTTP Server — wraps kokoro_tts.py as a REST API.
Listens on port 9881. CPU-only, lightweight.

POST /tts
  JSON: {"text": "...", "voice": "bm_george", "speed": 1.0}
  Returns: WAV audio file

GET /voices — list available voices
GET /health — health check
"""
import io
import os
import sys
import json
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import soundfile as sf

sys.path.insert(0, "/opt/tts-venv")
from kokoro_tts import Kokoro

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger("kokoro-server")

PORT = int(os.environ.get("KOKORO_PORT", "9881"))
engine = None


def get_engine():
    global engine
    if engine is None:
        log.info("Loading Kokoro model...")
        engine = Kokoro()
        log.info("Kokoro ready.")
    return engine


def list_voices():
    voice_dir = "/opt/tts-venv/kokoro_model/voices"
    voices = []
    for f in sorted(os.listdir(voice_dir)):
        if f.endswith(".bin"):
            voices.append(f[:-4])
    return voices


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info(fmt % args)

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok", "engine": "kokoro"})
        elif self.path == "/voices":
            self._json(200, {"voices": list_voices()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/tts":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        text = body.get("text", "")
        voice = body.get("voice", "bm_george")
        speed = float(body.get("speed", 1.0))

        if not text:
            self._json(400, {"error": "text required"})
            return

        try:
            k = get_engine()
            audio, sr = k.say(text, voice=voice, speed=speed)

            buf = io.BytesIO()
            sf.write(buf, audio, sr, format="WAV")
            wav_bytes = buf.getvalue()

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            self.send_header("X-Duration", f"{len(audio)/sr:.2f}")
            self.end_headers()
            self.wfile.write(wav_bytes)
            log.info(f"TTS: {len(text)} chars, voice={voice}, {len(audio)/sr:.1f}s")

        except Exception as e:
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    get_engine()  # warm up
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"Kokoro server listening on port {PORT}")
    server.serve_forever()
