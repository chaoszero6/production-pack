#!/usr/bin/env python3
"""Chatterbox TTS HTTP Server — wraps chatterbox_tts as a REST API.
Listens on port 9882. Requires GPU for voice cloning.

POST /tts
  multipart/form-data:
    text: "dialogue text"
    reference_audio: WAV file (5s voice reference)
    exaggeration: 0.5 (float, emotion exaggeration)
    cfg: 0.5 (float, classifier-free guidance)
  Returns: WAV audio file

POST /tts/json
  JSON: {"text": "...", "reference_audio_path": "/path/to/ref.wav"}
  Returns: WAV audio file

GET /health — health check
"""
import io
import os
import sys
import json
import logging
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, "/opt/tts/chatterbox-venv/lib/python3.13/site-packages")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
log = logging.getLogger("chatterbox-server")

PORT = int(os.environ.get("CHATTERBOX_PORT", "9882"))
model = None


def get_model():
    global model
    if model is None:
        log.info("Loading Chatterbox model (requires GPU)...")
        import torch
        from chatterbox import ChatterboxTTS
        model = ChatterboxTTS.from_pretrained(device=os.environ.get("CHATTERBOX_DEVICE", "cuda"))
        log.info("Chatterbox ready.")
    return model


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
            self._json(200, {"status": "ok", "engine": "chatterbox"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path in ("/tts", "/tts/json"):
            self._handle_json_tts()
        else:
            self._json(404, {"error": "not found"})

    def _handle_json_tts(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        text = body.get("text", "")
        ref_path = body.get("reference_audio_path", "")
        exaggeration = float(body.get("exaggeration", 0.5))
        cfg = float(body.get("cfg", 0.5))

        if not text:
            self._json(400, {"error": "text required"})
            return
        if not ref_path or not os.path.exists(ref_path):
            self._json(400, {"error": f"reference_audio_path not found: {ref_path}"})
            return

        self._generate(text, ref_path, exaggeration, cfg)

    def _generate(self, text, ref_path, exaggeration, cfg):
        try:
            import torch
            import torchaudio

            m = get_model()
            wav = m.generate(text, audio_prompt_path=ref_path,
                             exaggeration=exaggeration, cfg_weight=cfg)

            buf = io.BytesIO()
            torchaudio.save(buf, wav.cpu(), m.sr, format="wav")
            wav_bytes = buf.getvalue()

            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav_bytes)))
            duration = wav.shape[-1] / m.sr
            self.send_header("X-Duration", f"{duration:.2f}")
            self.end_headers()
            self.wfile.write(wav_bytes)
            log.info(f"TTS: {len(text)} chars, {duration:.1f}s")

        except Exception as e:
            log.error(f"Generation failed: {e}")
            self._json(500, {"error": str(e)})


if __name__ == "__main__":
    get_model()  # warm up
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info(f"Chatterbox server listening on port {PORT}")
    server.serve_forever()
