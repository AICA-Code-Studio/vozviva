"""Agenda del evento: qué charla hay en cada sala y a qué hora.

Con la agenda cargada, Vozviva:
- muestra a la audiencia el título y los oradores de la charla en curso;
- arma el glosario de cada sala (nombres de oradores + términos del título,
  el resumen y el glosario propio de la charla) para el reconocimiento;
- le pasa el contexto de la charla al traductor y a los resúmenes.

El archivo se relee solo si cambia, así se puede corregir durante el evento.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger("vozviva.agenda")

# Palabras con mayúscula que no aportan al glosario (inicio de frase, conectores comunes).
_STOP = {
    "el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o", "con", "para", "por", "como", "que",
    "cómo", "qué", "por qué", "the", "a", "an", "of", "in", "and", "or", "with", "for", "to", "how", "what", "why",
    "introducción", "introduction", "charla", "taller", "panel", "keynote",
}
_TOKEN = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9+#.\-]*[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9+#]|[A-Z]")


def extract_terms(text: str) -> list[str]:
    """Heurística simple: siglas (API, LLM), CamelCase (OpenTelemetry), palabras con
    dígitos (Python3, K8s) y palabras con mayúscula que no empiezan una oración."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?:])\s+|\n", text or ""):
        for i, m in enumerate(_TOKEN.finditer(sentence)):
            w = m.group(0).strip(".-")
            if len(w) < 2 or w.lower() in _STOP:
                continue
            acronym = w.isupper() and len(w) >= 2
            camel = bool(re.search(r"[a-z][A-Z]", w))
            digits = bool(re.search(r"\d", w)) and bool(re.search(r"[A-Za-z]", w))
            capital = w[0].isupper() and i > 0
            if (acronym or camel or digits or capital) and w not in out:
                out.append(w)
    return out


@dataclass
class Talk:
    id: str
    session: str
    start: datetime
    end: datetime
    title: str
    speakers: list[str] = field(default_factory=list)
    abstract: str = ""
    glossary: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "speakers": self.speakers,
            "start": self.start.isoformat(), "end": self.end.isoformat(),
        }

    def terms(self) -> list[str]:
        seen: list[str] = []
        for t in [*self.speakers, *self.glossary, *extract_terms(self.title), *extract_terms(self.abstract)]:
            if t and t not in seen:
                seen.append(t)
        return seen

    def context(self) -> str:
        parts = [f"Charla: {self.title}."]
        if self.speakers:
            parts.append("Oradores: " + ", ".join(self.speakers) + ".")
        if self.abstract:
            parts.append("Resumen del programa: " + self.abstract.strip()[:600])
        return " ".join(parts)


class Agenda:
    def __init__(self, path: str | None, tz: str, session_ids: list[str]) -> None:
        self.path = Path(path) if path else None
        self.tz = ZoneInfo(tz)
        self.session_ids = set(session_ids)
        self.talks: list[Talk] = []
        self.error: str | None = None
        self._mtime: float | None = None
        self._checked = 0.0
        if self.path:
            self._load()

    # -- carga -----------------------------------------------------------------
    def _parse_dt(self, v: Any) -> datetime:
        if isinstance(v, int):  # YAML lee "10:30" sin comillas como sexagesimal (630)
            v = f"{v // 60:02d}:{v % 60:02d}"
        if isinstance(v, str) and re.fullmatch(r"\d{1,2}:\d{2}", v.strip()):
            # Solo hora: se toma el día de hoy (práctico para eventos de un día y para demos).
            h, m = map(int, v.strip().split(":"))
            return self.now().replace(hour=h, minute=m, second=0, microsecond=0)
        if isinstance(v, datetime):
            dt = v
        else:
            dt = datetime.fromisoformat(str(v).strip().replace(" ", "T", 1))
        return dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt

    def _load(self) -> None:
        assert self.path is not None
        try:
            self._mtime = self.path.stat().st_mtime
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or []
            items = raw.get("talks", []) if isinstance(raw, dict) else raw
            talks = []
            for i, it in enumerate(items):
                t = Talk(
                    id=str(it.get("id") or f"charla-{i + 1}"),
                    session=str(it["session"]),
                    start=self._parse_dt(it["start"]),
                    end=self._parse_dt(it["end"]),
                    title=str(it.get("title", "")).strip(),
                    speakers=[str(x) for x in it.get("speakers", []) or []],
                    abstract=str(it.get("abstract", "") or ""),
                    glossary=[str(x) for x in it.get("glossary", []) or []],
                )
                if t.session not in self.session_ids:
                    log.warning("La charla %r apunta a una sala inexistente: %s", t.title, t.session)
                if t.end <= t.start:
                    raise ValueError(f"La charla {t.title!r} termina antes de empezar")
                talks.append(t)
            self.talks = sorted(talks, key=lambda t: t.start)
            self.error = None
            log.info("Agenda cargada: %d charlas", len(self.talks))
        except Exception as e:  # noqa: BLE001 - una agenda rota no debe cortar los subtítulos
            self.error = f"{type(e).__name__}: {e}"
            log.error("No se pudo cargar la agenda %s: %s", self.path, e)

    def maybe_reload(self) -> None:
        if not self.path or time.monotonic() - self._checked < 10:
            return
        self._checked = time.monotonic()
        try:
            if self.path.stat().st_mtime != self._mtime:
                self._load()
        except OSError as e:
            self.error = str(e)

    # -- consultas ---------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def current(self, session: str, at: datetime | None = None) -> Talk | None:
        self.maybe_reload()
        at = at or self.now()
        for t in self.talks:
            if t.session == session and t.start <= at < t.end:
                return t
        return None

    def next(self, session: str, at: datetime | None = None) -> Talk | None:
        self.maybe_reload()
        at = at or self.now()
        return next((t for t in self.talks if t.session == session and t.start > at), None)

    def relevant(self, session: str, at: datetime | None = None) -> Talk | None:
        """La charla en curso o, si falta poco, la próxima (para preparar el glosario)."""
        at = at or self.now()
        cur = self.current(session, at)
        if cur:
            return cur
        nxt = self.next(session, at)
        return nxt if nxt and nxt.start - at <= timedelta(minutes=15) else None

    def talk(self, talk_id: str) -> Talk | None:
        self.maybe_reload()
        return next((t for t in self.talks if t.id == talk_id), None)
