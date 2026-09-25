"""Prueba rápida de gemini-3.5-transcribe-live con tu API key.

Uso:  GEMINI_API_KEY=... python scripts/probar_gemini.py charla.mp3 [--segundos 60] [--idioma en-US]

Manda el audio a velocidad real y muestra los mensajes crudos (interim y final)
con su tiempo de llegada. Sirve para confirmar el formato de las respuestas y
medir la latencia antes del evento.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time

from google import genai
from google.genai import types

CHUNK = 3200  # 100 ms de PCM 16 kHz mono s16le


def load_pcm(path: str, seconds: float) -> bytes:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-t", str(seconds),
           "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        msg = r.stderr.decode(errors="replace").strip() or f"código {r.returncode}"
        sys.exit(f"ffmpeg no pudo leer {path!r}: {msg}")
    if not r.stdout:
        sys.exit(f"ffmpeg no encontró audio en {path!r}")
    return r.stdout


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("--segundos", type=float, default=60)
    ap.add_argument("--idioma", default="", help="BCP-47, p. ej. en-US; vacío = detección automática")
    ap.add_argument("--modelo", default="gemini-3.5-transcribe-live")
    a = ap.parse_args()
    if not os.path.isfile(a.audio):
        sys.exit(f"No encuentro el archivo {a.audio!r} en {os.getcwd()}. Revisá el nombre con `dir *.mp3`.")

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Falta GEMINI_API_KEY")
    pcm = load_pcm(a.audio, a.segundos)
    client = genai.Client(api_key=key)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.TEXT],
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=[a.idioma] if a.idioma else [], mode="SMART"),
    )
    t0 = time.monotonic()
    async with client.aio.live.connect(model=a.modelo, config=config) as session:
        async def send() -> None:
            for i in range(0, len(pcm), CHUNK):
                await session.send_realtime_input(audio=types.Blob(data=pcm[i:i + CHUNK], mime_type="audio/pcm;rate=16000"))
                await asyncio.sleep(0.1)
            await session.send_realtime_input(audio_stream_end=True)

        sender = asyncio.create_task(send())
        try:
            while True:
                async for msg in session.receive():
                    sc = msg.server_content
                    t = time.monotonic() - t0
                    if sc and sc.interim_input_transcription:
                        print(f"{t:6.1f}s INTERIM  {sc.interim_input_transcription.text!r}")
                    if sc and sc.input_transcription:
                        tr = sc.input_transcription
                        print(f"{t:6.1f}s FINAL    {tr.text!r} lang={tr.language_code} finished={tr.finished}")
                    if msg.go_away:
                        print(f"{t:6.1f}s GO_AWAY  {msg.go_away}")
                if sender.done() and time.monotonic() - t0 > a.segundos + 5:
                    break
        finally:
            sender.cancel()


if __name__ == "__main__":
    asyncio.run(main())
