""""Tocá una frase y te la explico": explicación personal de una línea de subtítulo.

Cada explicación se guarda por (sala, canal, segmento): aunque muchas personas
toquen la misma frase, se genera una sola vez. El costo queda acotado por la
cantidad de frases, no por la cantidad de personas.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import Settings, target_desc
from .metrics import METER

SYSTEM = """Ayudás a alguien del público de una charla técnica que no entendió una frase de los subtítulos.
Explicá esa frase para que la entienda sin conocimientos previos. Reglas:
- explanation: 1 a 3 oraciones simples que digan qué quiso decir el orador, usando el contexto de la charla.
- terms: hasta 4 términos técnicos, siglas o nombres que aparezcan en la frase, cada uno con un significado breve y concreto para este contexto. Si no hay, lista vacía.
- No agregues información que no se desprenda de la frase, el contexto o el conocimiento general del término. No opines sobre el orador.
Respondé solo JSON: {"explanation": "...", "terms": [{"term": "...", "meaning": "..."}]}"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "explanation": {"type": "STRING"},
        "terms": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "term": {"type": "STRING"}, "meaning": {"type": "STRING"}}, "required": ["term", "meaning"]}},
    },
    "required": ["explanation", "terms"],
}

# Explicaciones de ejemplo para el modo demo (sin API key).
DEMO_TERMS = {
    "observabilidad": "Poder saber qué pasa adentro de un sistema mirando los datos que produce: registros, métricas y trazas.",
    "observability": "Poder saber qué pasa adentro de un sistema mirando los datos que produce: registros, métricas y trazas.",
    "sistemas distribuidos": "Programas que funcionan repartidos en varias computadoras que se comunican entre sí.",
    "distributed systems": "Programas que funcionan repartidos en varias computadoras que se comunican entre sí.",
    "opentelemetry": "Estándar libre para recolectar métricas, registros y trazas de las aplicaciones.",
    "grafana": "Herramienta libre para armar tableros con gráficos a partir de métricas.",
    "kubernetes": "Sistema libre que organiza y ejecuta aplicaciones en contenedores sobre muchas computadoras.",
    "monolito": "Aplicación armada como un único programa grande, en lugar de varias piezas separadas.",
    "monolith": "Aplicación armada como un único programa grande, en lugar de varias piezas separadas.",
    "pruebas de carga": "Pruebas que simulan mucha gente usando el sistema a la vez, para ver cuánto aguanta.",
    "load tests": "Pruebas que simulan mucha gente usando el sistema a la vez, para ver cuánto aguanta.",
    "licencia libre": "Permiso que deja usar, estudiar, modificar y compartir el código.",
    "github": "Sitio donde se publica y comparte código.",
    "optimizar": "Hacer que algo funcione más rápido o gaste menos recursos.",
    "optimize": "Hacer que algo funcione más rápido o gaste menos recursos.",
}


def _prompt(text: str, before: list[str], target: str, talk: str) -> str:
    parts = [f"Escribí la respuesta en: {target_desc(target)}."]
    if talk:
        parts.append(talk)
    if before:
        parts.append("Lo que se dijo justo antes (solo contexto):\n" + "\n".join(before))
    parts.append(f"Frase a explicar:\n{text}")
    return "\n".join(parts)


def _parse(raw: str) -> dict[str, Any]:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    terms = [
        {"term": str(t.get("term", "")).strip(), "meaning": str(t.get("meaning", "")).strip()}
        for t in data.get("terms", []) if isinstance(t, dict) and t.get("term") and t.get("meaning")
    ][:4]
    return {"explanation": str(data.get("explanation", "")).strip(), "terms": terms}


def demo_explain(text: str) -> dict[str, Any]:
    terms = []
    for k, v in DEMO_TERMS.items():
        m = re.search(rf"\b{re.escape(k)}\b", text, re.IGNORECASE)
        if m:
            terms.append({"term": m.group(0), "meaning": v})  # como aparece en la frase
    seen: set[str] = set()
    unique = [t for t in terms if not (t["meaning"] in seen or seen.add(t["meaning"]))]  # type: ignore[func-returns-value]
    return {"explanation": "", "terms": unique[:4]}


class Explainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.model_summary or settings.model_translate
        self.mode = "demo"
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

    async def explain(self, session: str, text: str, before: list[str], target: str, talk: str) -> tuple[dict[str, Any], str]:
        if self.mode == "gemini":
            from google.genai import types

            r = await self.client.aio.models.generate_content(
                model=self.model,
                contents=_prompt(text, before, target, talk),
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
                "format": {"type": "object", "properties": {
                    "explanation": {"type": "string"},
                    "terms": {"type": "array", "items": {"type": "object", "properties": {
                        "term": {"type": "string"}, "meaning": {"type": "string"}}, "required": ["term", "meaning"]}},
                }, "required": ["explanation", "terms"]},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": _prompt(text, before, target, talk)}],
            })
            r.raise_for_status()
            return _parse(r.json()["message"]["content"]), self.mode
        return demo_explain(text), self.mode
