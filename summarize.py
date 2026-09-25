"""Resúmenes "¿Qué me perdí?": lo dicho en los últimos minutos de una sala."""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import Settings, target_desc
from .metrics import METER

log = logging.getLogger("vozviva.summarize")

SYSTEM = """Resumís una charla técnica en vivo para alguien que se perdió los últimos minutos.
Recibís la transcripción automática de ese tramo (puede tener errores de reconocimiento).
Reglas:
- Entre 2 y 5 puntos, cada uno una oración corta y concreta. Si hay poco contenido, menos puntos.
- Solo lo que está en la transcripción: no inventes datos, nombres ni conclusiones.
- Priorizá ideas, decisiones, ejemplos y recomendaciones; no describas la charla ("el orador habló de...").
- Mantené nombres propios, productos y código tal cual.
Respondé solo JSON: {"bullets": ["...", "..."]}"""

SCHEMA = {"type": "OBJECT", "properties": {"bullets": {"type": "ARRAY", "items": {"type": "STRING"}}}, "required": ["bullets"]}


def _prompt(lines: list[str], minutes: int, target: str, talk: str) -> str:
    parts = [f"Escribí el resumen en: {target_desc(target)}.", f"Tramo: últimos {minutes} minutos."]
    if talk:
        parts.append(talk)
    parts.append("Transcripción:\n" + "\n".join(lines))
    return "\n".join(parts)


def _parse(raw: str) -> list[str]:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    return [str(b).strip() for b in data.get("bullets", []) if str(b).strip()][:5]


def extractive(lines: list[str], n: int = 4) -> list[str]:
    """Sin IA: frases textuales repartidas en el tramo (modo demo o sin API key)."""
    lines = list(dict.fromkeys(lines))  # sin repetidas, en orden
    if len(lines) <= n:
        return lines
    step = (len(lines) - 1) / (n - 1)
    return [lines[round(i * step)] for i in range(n)]


class Summarizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.model_summary or settings.model_translate
        self.mode = "extractivo"
        self.client: Any = None
        self.http: Any = None
        if settings.translator == "ollama" or settings.backend == "local":
            import httpx

            self.http = httpx.AsyncClient(base_url=settings.ollama_url, timeout=60.0)
            self.mode = "ollama"
        elif settings.gemini_api_key and settings.backend != "mock":
            from google import genai

            self.client = genai.Client(api_key=settings.gemini_api_key)
            self.mode = "gemini"

    async def summarize(self, session: str, lines: list[str], minutes: int, target: str, talk: str) -> tuple[list[str], str]:
        if not lines:
            return [], self.mode
        if self.mode == "gemini":
            from google.genai import types

            r = await self.client.aio.models.generate_content(
                model=self.model,
                contents=_prompt(lines, minutes, target, talk),
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM, temperature=0.2,
                    response_mime_type="application/json", response_schema=SCHEMA,
                ),
            )
            METER.add(session, self.model, getattr(r, "usage_metadata", None))
            return _parse(r.text or "{}"), self.mode
        if self.mode == "ollama":
            r = await self.http.post("/api/chat", json={
                "model": self.settings.ollama_model, "stream": False, "options": {"temperature": 0.2},
                "format": {"type": "object", "properties": {"bullets": {"type": "array", "items": {"type": "string"}}}, "required": ["bullets"]},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(lines, minutes, target, talk)}],
            })
            r.raise_for_status()
            return _parse(r.json()["message"]["content"]), self.mode
        return extractive(lines), self.mode
