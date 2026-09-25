"""Modelos de datos que circulan por el sistema."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field


@dataclass
class TranscriptEvent:
    """Lo que emite un backend de reconocimiento para una sesión.

    `seg` identifica un fragmento de habla. Un mismo `seg` puede llegar varias
    veces como parcial (`final=False`) y termina con un único evento final.
    Si el backend ya tradujo el fragmento, lo manda en `translations`.
    """

    seg: int
    text: str
    final: bool
    t0: float
    t1: float
    lang: str | None = None
    translations: dict[str, str] = field(default_factory=dict)
    # Algunos backends (live translate) emiten directamente un canal traducido.
    channel: str = "orig"
    # Corrección de un final ya publicado (mismo `seg`); `previous` es el texto anterior.
    revision: bool = False
    previous: str | None = None


@dataclass
class Caption:
    """Unidad que se publica hacia la audiencia."""

    session: str
    channel: str
    seg: int
    text: str
    final: bool
    t0: float
    t1: float
    lang: str
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "Caption":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
