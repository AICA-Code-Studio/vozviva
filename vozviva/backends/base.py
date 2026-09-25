"""Interfaz común de los backends de reconocimiento."""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
from abc import ABC, abstractmethod
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from ..config import SessionConfig, Settings
from ..models import TranscriptEvent

Emit = Callable[[TranscriptEvent], Awaitable[None]]
T = TypeVar("T")
log = logging.getLogger("vozviva.backends")


class Backend(ABC):
    """Consume audio PCM 16 kHz mono de UNA sesión y emite TranscriptEvent.

    Los backends que además traducen completan `translations` en los eventos
    finales; si no, el worker traduce con el Translator configurado.
    """

    #: True si el backend entrega traducciones en los eventos finales
    translates = False
    #: False si los tiempos de los eventos no salen del audio real (mock)
    measures_latency = True

    def __init__(self, settings: Settings, session: SessionConfig) -> None:
        self.settings = settings
        self.session = session
        self._seg_counter = itertools.count(1)
        self.status: dict[str, object] = {}
        # El worker los reemplaza para usar la agenda (glosario y contexto de la charla en curso).
        self.glossary_fn: Callable[[], list[str]] = lambda: list(settings.glossary)
        self.context_fn: Callable[[], str] = lambda: ""

    def provided_channels(self) -> set[str]:
        """Canales que este backend traduce por su cuenta; el resto lo traduce el worker."""
        if not self.translates:
            return set()
        return {c for c in self.session.channels if c != "orig"}

    def next_seg(self) -> int:
        return next(self._seg_counter)

    @abstractmethod
    async def run(self, audio: AsyncIterator[bytes], emit: Emit) -> None:
        """Corre hasta que se cancele. Puede lanzar excepciones: el worker reintenta."""


async def with_retries(fn: Callable[[], Awaitable[T]], attempts: int = 3, base: float = 0.6, what: str = "") -> T:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - la API puede fallar de muchas formas
            last = e
            wait = base * (2 ** i) + random.random() * 0.3
            log.warning("%s falló (intento %d/%d): %s", what or "llamada", i + 1, attempts, e)
            await asyncio.sleep(wait)
    assert last is not None
    raise last
