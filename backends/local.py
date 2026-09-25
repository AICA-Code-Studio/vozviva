"""Backend 100 % local: VAD + faster-whisper para reconocer, y el traductor
configurado (recomendado: `translator: ollama` con un modelo Gemma) para traducir.

Requiere `pip install faster-whisper` y, para traducir, Ollama corriendo con un
modelo Gemma descargado. No envía audio ni texto fuera de la máquina.
Estado: experimental, no probado en un evento real.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, AsyncIterator

import numpy as np

from ..audio import Segment, Segmenter
from ..models import TranscriptEvent
from .base import Backend, Emit

log = logging.getLogger("vozviva.local")

_models: dict[tuple[str, str], Any] = {}
_lock = threading.Lock()


def _get_model(name: str, device: str) -> Any:
    from faster_whisper import WhisperModel  # import diferido

    key = (name, device)
    with _lock:
        if key not in _models:
            compute = "float16" if device == "cuda" else "int8"
            _models[key] = WhisperModel(name, device=device, compute_type=compute if device != "auto" else "default")
        return _models[key]


class LocalBackend(Backend):
    translates = False

    def __init__(self, settings, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, session)
        self.model = _get_model(settings.whisper_model, settings.whisper_device)
        # Un reconocimiento a la vez por sesión; varias sesiones comparten el modelo.
        self.sem = asyncio.Semaphore(1)

    async def run(self, audio: AsyncIterator[bytes], emit: Emit) -> None:
        seg = Segmenter(min_silence_ms=self.settings.min_silence_ms, max_segment_s=self.settings.max_segment_s)
        tasks: set[asyncio.Task[None]] = set()
        try:
            async for chunk in audio:
                for s in seg.feed(chunk):
                    t = asyncio.create_task(self._process(self.next_seg(), s, emit))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
        finally:
            for t in tasks:
                t.cancel()

    async def _process(self, seg_id: int, s: Segment, emit: Emit) -> None:
        async with self.sem:
            text, lang = await asyncio.to_thread(self._transcribe, s)
        if text:
            await emit(TranscriptEvent(seg=seg_id, text=text, final=True, t0=s.t0, t1=s.t1, lang=lang))

    def _transcribe(self, s: Segment) -> tuple[str, str]:
        audio = np.frombuffer(s.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        lang = None if self.session.source_lang == "auto" else self.session.source_lang
        prompt = ", ".join(self.glossary_fn()[:50]) or None
        segments, info = self.model.transcribe(
            audio, language=lang, beam_size=1, vad_filter=False, initial_prompt=prompt, condition_on_previous_text=False
        )
        text = " ".join(x.text.strip() for x in segments).strip()
        return text, (info.language or self.session.source_lang)
