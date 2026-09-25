"""Arma oraciones a partir de fragmentos de texto que llegan en streaming.

Los modelos en vivo devuelven la transcripción de a pedazos ("Hola a", " todos.",
" Hoy vamos"). Para subtitular conviene publicar un parcial que se va
actualizando y cerrarlo como final en un borde natural: fin de oración, pausa,
o largo máximo (para que un subtítulo no crezca sin límite).
"""
from __future__ import annotations

import re
import time
from typing import Awaitable, Callable

from .models import TranscriptEvent

_SENTENCE_END = re.compile(r"[.!?…。？！](?:[\"'”»)\]]*)\s+")


class SentenceAssembler:
    def __init__(
        self,
        emit: Callable[[TranscriptEvent], Awaitable[None]],
        next_seg: Callable[[], int],
        channel: str = "orig",
        lang: str | None = None,
        max_chars: int = 200,
        idle_s: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.emit = emit
        self.next_seg = next_seg
        self.channel = channel
        self.lang = lang
        self.max_chars = max_chars
        self.idle_s = idle_s
        self.clock = clock
        self.buf = ""
        self.seg: int | None = None
        self.t0 = 0.0
        self.t1 = 0.0
        self.last_add = 0.0

    async def add(self, text: str, t: float) -> None:
        if not text:
            return
        if self.seg is None:
            self.seg = self.next_seg()
            self.t0 = t
        self.buf += text
        self.t1 = t
        self.last_add = self.clock()

        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m or not self.buf[m.end():].strip():
                break
            head, rest = self.buf[: m.end()], self.buf[m.end():]
            await self._commit(head)
            self.buf = rest.lstrip()
            self.seg = self.next_seg()
            self.t0 = t

        if len(self.buf) > self.max_chars:
            cut = max(self.buf.rfind(", ", 0, self.max_chars), self.buf.rfind(" ", 0, self.max_chars))
            if cut <= 0:
                cut = self.max_chars
            head, rest = self.buf[: cut + 1], self.buf[cut + 1:]
            await self._commit(head)
            self.buf = rest.lstrip()
            self.seg = self.next_seg() if self.buf else None
            self.t0 = t

        if self.buf.strip() and self.seg is not None:
            await self.emit(self._event(self.buf, final=False))

    async def tick(self) -> None:
        """Cierra el parcial si no llegó texto nuevo en `idle_s` (pausa del orador)."""
        if self.buf.strip() and self.clock() - self.last_add >= self.idle_s:
            await self.flush()

    async def flush(self) -> None:
        if self.buf.strip():
            await self._commit(self.buf)
        self.buf = ""
        self.seg = None

    async def _commit(self, text: str) -> None:
        text = text.strip()
        if text and self.seg is not None:
            await self.emit(self._event(text, final=True))

    def _event(self, text: str, final: bool) -> TranscriptEvent:
        assert self.seg is not None
        return TranscriptEvent(
            seg=self.seg, text=text.strip(), final=final, t0=self.t0, t1=self.t1,
            lang=self.lang, channel=self.channel,
        )
