"""Backend por fragmentos: VAD local + generate_content con audio.

Cada fragmento de habla (0,5 a 8 s) se manda a un modelo multimodal que en una
sola llamada devuelve transcripción, idioma y traducciones. Tiene más latencia
que la Live API (duración del fragmento + ~1 s) pero no depende de conexiones
largas, así que sirve como respaldo si la Live API falla o cambia.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any, AsyncIterator

from ..audio import Segment, Segmenter, pcm_to_wav
from ..config import target_desc
from ..metrics import METER
from ..models import TranscriptEvent
from .base import Backend, Emit, with_retries

log = logging.getLogger("vozviva.gemini_chunked")

SYSTEM = """Sos el motor de subtítulos en vivo de una conferencia técnica.
Recibís un fragmento corto de audio de un escenario. Respondé solo JSON con:
- transcript: transcripción fiel de lo que se dice, en el idioma hablado, con puntuación; sin muletillas ni repeticiones.
- lang: código ISO 639-1 del idioma hablado.
- translations: la traducción del transcript a cada idioma pedido, natural y breve, apta para subtítulos.
Reglas: no inventes nada que no esté en el audio. Si no hay habla inteligible (silencio, música, aplausos), devolvé transcript vacío y traducciones vacías.
Mantené sin traducir nombres propios, productos, comandos y código. El fragmento puede empezar o terminar a mitad de frase: transcribí solo lo que se oye."""


def build_schema(targets: list[str]) -> dict[str, Any]:
    return {
        "type": "OBJECT",
        "properties": {
            "transcript": {"type": "STRING"},
            "lang": {"type": "STRING"},
            "translations": {
                "type": "OBJECT",
                "properties": {t: {"type": "STRING"} for t in targets},
                "required": targets,
            },
        },
        "required": ["transcript", "lang", "translations"],
    }


def parse_response(text: str, targets: list[str]) -> tuple[str, str | None, dict[str, str]]:
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(cleaned)
    transcript = (data.get("transcript") or "").strip()
    lang = (data.get("lang") or "").strip().lower()[:5] or None
    tr = data.get("translations") or {}
    translations = {t: (tr.get(t) or "").strip() for t in targets if isinstance(tr, dict)}
    return transcript, lang, translations


class GeminiChunkedBackend(Backend):
    translates = True

    def __init__(self, settings, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, session)
        from google import genai
        from google.genai import types

        if not settings.gemini_api_key:
            raise RuntimeError("Falta GEMINI_API_KEY para usar el backend gemini_chunked")
        self.types = types
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.targets = [c for c in session.channels if c != "orig"]
        self.sem = asyncio.Semaphore(settings.max_inflight)
        self.context: deque[str] = deque(maxlen=3)
        self.status.update({"pending": 0, "segments": 0, "last_error": None})

    async def run(self, audio: AsyncIterator[bytes], emit: Emit) -> None:
        seg = Segmenter(min_silence_ms=self.settings.min_silence_ms, max_segment_s=self.settings.max_segment_s)
        tasks: set[asyncio.Task[None]] = set()
        try:
            async for chunk in audio:
                for s in seg.feed(chunk):
                    t = asyncio.create_task(self._process(self.next_seg(), s, emit))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                self.status["speaking"] = seg.speaking
        finally:
            for t in tasks:
                t.cancel()

    async def _process(self, seg_id: int, s: Segment, emit: Emit) -> None:
        self.status["pending"] = int(self.status["pending"]) + 1  # type: ignore[arg-type]
        try:
            async with self.sem:
                transcript, lang, translations = await with_retries(lambda: self._call(s), what=f"{self.session.id}#{seg_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.status["last_error"] = f"{type(e).__name__}: {e}"[:300]
            return
        finally:
            self.status["pending"] = int(self.status["pending"]) - 1  # type: ignore[arg-type]
        if not transcript:
            return
        self.status["segments"] = int(self.status["segments"]) + 1  # type: ignore[arg-type]
        self.context.append(transcript)
        await emit(TranscriptEvent(
            seg=seg_id, text=transcript, final=True, t0=s.t0, t1=s.t1,
            lang=lang or self.session.source_lang,
            translations={k: v for k, v in translations.items() if v},
        ))

    async def _call(self, s: Segment) -> tuple[str, str | None, dict[str, str]]:
        t = self.types
        src = self.session.source_lang
        targets_txt = "; ".join(f"{c} = {target_desc(c)}" for c in self.targets) or "ninguno"
        prompt = [f"Idioma esperado: {src}. Traducir a: {targets_txt}."]
        talk = self.context_fn()
        if talk:
            prompt.append(talk)
        if self.context:
            prompt.append("Contexto previo (solo referencia, no lo repitas): " + " ".join(self.context))
        glossary = self.glossary_fn()
        if glossary:
            prompt.append("Glosario (escribir así): " + ", ".join(glossary[:200]))
        cfg = t.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=build_schema(self.targets) if self.targets else None,
        )
        resp = await self.client.aio.models.generate_content(
            model=self.settings.model_chunked,
            contents=[
                t.Part.from_bytes(data=pcm_to_wav(s.pcm), mime_type="audio/wav"),
                "\n".join(prompt),
            ],
            config=cfg,
        )
        METER.add(self.session.id, self.settings.model_chunked, getattr(resp, "usage_metadata", None))
        return parse_response(resp.text or "{}", self.targets)
