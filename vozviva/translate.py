"""Traducción de segmentos finales para los backends que solo transcriben."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from .backends.base import with_retries
from .config import Settings, target_desc
from .metrics import METER

log = logging.getLogger("vozviva.translate")

SYSTEM = """Sos subtitulador profesional en una conferencia técnica en vivo.
Traducí el segmento indicado a cada idioma pedido. Reglas:
- Traducción natural, fiel y breve, apta para leer en un subtítulo.
- No agregues nada que no esté en el original, no expliques, no resumas.
- Mantené sin traducir nombres propios, productos, comandos y código.
- Si el segmento ya está en el idioma destino, devolvelo igual (corrigiendo solo puntuación).
- El contexto previo es solo para desambiguar; no lo traduzcas.
Respondé solo JSON con una clave por código de idioma."""


def _prompt(text: str, src: str, targets: list[str], context: list[tuple[str, str]], glossary: list[str], talk: str = "") -> str:
    lines = [f"Idioma de origen: {src}.", "Destinos (clave = descripción): " + "; ".join(f"{t} = {target_desc(t)}" for t in targets) + "."]
    if talk:
        lines.append(talk)
    if glossary:
        lines.append("Glosario (no traducir / escribir así): " + ", ".join(glossary[:200]))
    if context:
        lines.append("Contexto previo:")
        lines += [f"- {c}" for c, _ in context]
    lines.append(f"Segmento a traducir:\n{text}")
    return "\n".join(lines)


def _schema(targets: list[str]) -> dict[str, Any]:
    return {"type": "OBJECT", "properties": {t: {"type": "STRING"} for t in targets}, "required": targets}


def _parse(raw: str, targets: list[str]) -> dict[str, str]:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    return {t: str(data.get(t, "")).strip() for t in targets if data.get(t)}


class Translator(ABC):
    @abstractmethod
    async def translate(
        self, text: str, src: str, targets: list[str], context: list[tuple[str, str]],
        glossary: list[str] | None = None, talk: str = "", session: str = "",
    ) -> dict[str, str]:
        ...


class NoTranslator(Translator):
    async def translate(self, text, src, targets, context, glossary=None, talk="", session=""):  # type: ignore[no-untyped-def]
        return {}


class GeminiTranslator(Translator):
    def __init__(self, settings: Settings) -> None:
        from google import genai
        from google.genai import types

        if not settings.gemini_api_key:
            raise RuntimeError("Falta GEMINI_API_KEY para el traductor de Gemini")
        self.types = types
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.settings = settings

    async def translate(self, text, src, targets, context, glossary=None, talk="", session=""):  # type: ignore[no-untyped-def]
        t = self.types
        glossary = self.settings.glossary if glossary is None else glossary
        cfg = t.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=_schema(targets),
        )

        async def call() -> dict[str, str]:
            r = await self.client.aio.models.generate_content(
                model=self.settings.model_translate,
                contents=_prompt(text, src, targets, context, glossary, talk),
                config=cfg,
            )
            METER.add(session, self.settings.model_translate, getattr(r, "usage_metadata", None))
            return _parse(r.text or "{}", targets)

        return await with_retries(call, what="traducción")


class OllamaTranslator(Translator):
    """Traducción local con Ollama (p. ej. Gemma)."""

    def __init__(self, settings: Settings) -> None:
        import httpx

        self.settings = settings
        self.http = httpx.AsyncClient(base_url=settings.ollama_url, timeout=30.0)

    async def translate(self, text, src, targets, context, glossary=None, talk="", session=""):  # type: ignore[no-untyped-def]
        glossary = self.settings.glossary if glossary is None else glossary
        schema = {
            "type": "object",
            "properties": {t: {"type": "string"} for t in targets},
            "required": targets,
        }

        async def call() -> dict[str, str]:
            r = await self.http.post("/api/chat", json={
                "model": self.settings.ollama_model,
                "stream": False,
                "format": schema,
                "options": {"temperature": 0.1},
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": _prompt(text, src, targets, context, glossary, talk)},
                ],
            })
            r.raise_for_status()
            return _parse(r.json()["message"]["content"], targets)

        return await with_retries(call, what="traducción local")


def make_translator(settings: Settings) -> Translator:
    if settings.translator == "gemini":
        return GeminiTranslator(settings)
    if settings.translator == "ollama":
        return OllamaTranslator(settings)
    if settings.translator == "none":
        return NoTranslator()
    raise ValueError(f"Traductor desconocido: {settings.translator!r} (gemini | ollama | none)")
