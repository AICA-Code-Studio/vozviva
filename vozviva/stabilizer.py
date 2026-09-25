"""Estabilizador de parciales para transcripción en streaming.

Problema (visto con audio real): `gemini-3.5-transcribe-live` manda parciales
acumulados cada ~0,5 s, pero solo cierra la frase (FINAL) cuando el orador hace
una pausa. Un orador que no hace pausas puede producir un solo FINAL por minuto:
en pantalla quedaría un párrafo que crece sin parar y la traducción llegaría
un minuto tarde.

Solución: cerramos nosotros cada oración cuando ya está estable en los parciales
(no cambió entre dos parciales seguidos y el orador siguió hablando después),
así se publica y se traduce enseguida. Cuando llega el FINAL del modelo, que es
más preciso, lo alineamos con lo ya publicado: si una oración cambió, se
republica con el mismo número de segmento (la pantalla la reemplaza) y el resto
se publica como oraciones nuevas.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable, NamedTuple

_END = re.compile(r"[.!?…。？！][\"'”»)\]]*$")
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)


class Action(NamedTuple):
    kind: str            # "partial" | "final"
    seg: int
    text: str
    revision: bool = False
    previous: str | None = None  # texto anterior, si es una corrección


def _norm(w: str) -> str:
    return _PUNCT.sub("", w.lower())


def norm_text(text: str) -> str:
    return " ".join(x for x in (_norm(w) for w in text.split()) if x)


def _lcp(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _mapper(src: list[str], dst: list[str]) -> Callable[[int], int]:
    """Traduce una posición en `src` a la posición equivalente en `dst`."""
    blocks = [b for b in SequenceMatcher(None, [_norm(w) for w in src], [_norm(w) for w in dst], autojunk=False)
              .get_matching_blocks() if b.size]

    def mp(i: int) -> int:
        best = 0
        for a, b, size in blocks:
            if a <= i <= a + size:
                best = b + (i - a)
            elif a + size < i:
                best = b + size
        return min(best, len(dst))

    return mp


def split_sentences(words: list[str], max_words: int = 40) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        cur.append(w)
        if _END.search(w) or len(cur) >= max_words:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


class UtteranceStabilizer:
    def __init__(self, next_seg: Callable[[], int], min_tail_words: int = 3, max_words: int = 30) -> None:
        self.next_seg = next_seg
        self.min_tail_words = min_tail_words
        self.max_words = max_words
        self.reset()

    def reset(self) -> None:
        self.seg: int | None = None
        self.prev: list[str] = []
        self.base: list[str] = []          # parcial en el momento del último cierre
        self.committed_words = 0           # posición de corte dentro de `base`
        self.committed: list[tuple[int, str]] = []  # (segmento, texto) ya publicados como finales
        self.pending_final = ""

    @property
    def active(self) -> bool:
        return self.seg is not None

    def _start_in(self, words: list[str]) -> int:
        if not self.committed_words:
            return 0
        return _mapper(self.base, words)(self.committed_words)

    def interim(self, text: str) -> list[Action]:
        words = text.split()
        if not words:
            return []
        if self.seg is None:
            self.seg = self.next_seg()
        stable = _lcp(self.prev, words)
        self.prev = words
        start = self._start_in(words)
        actions: list[Action] = []
        while stable > start:
            cut = None
            for i in range(start, stable):
                if _END.search(words[i]) and len(words) - (i + 1) >= self.min_tail_words:
                    cut = i + 1
                    break
            if cut is None and stable - start >= self.max_words:
                # Sin puntuación: cortamos en la última coma del tramo estable, o por largo.
                commas = [i for i in range(start + 8, start + self.max_words) if words[i].endswith(",")]
                cut = (commas[-1] + 1) if commas else start + self.max_words - 5
            if cut is None:
                break
            chunk = " ".join(words[start:cut])
            actions.append(Action("final", self.seg, chunk))
            self.committed.append((self.seg, chunk))
            self.base, self.committed_words = words, cut
            self.seg = self.next_seg()
            start = cut
        rest = words[start:]
        if rest:
            actions.append(Action("partial", self.seg, " ".join(rest)))
        return actions

    def final(self, text: str) -> list[Action]:
        """FINAL del modelo para toda la frase: corrige lo publicado y publica lo que falta."""
        final_words = text.split()
        actions: list[Action] = []
        committed_words: list[str] = []
        bounds: list[tuple[int, int, int, str]] = []
        for seg, chunk in self.committed:
            w = chunk.split()
            bounds.append((seg, len(committed_words), len(committed_words) + len(w), chunk))
            committed_words += w
        mp = _mapper(committed_words, final_words) if committed_words else (lambda i: 0)
        for seg, s, e, chunk in bounds:
            new = " ".join(final_words[mp(s):mp(e)])
            if new and new != chunk:
                actions.append(Action("final", seg, new, revision=True, previous=chunk))
        rest = final_words[mp(len(committed_words)):] if committed_words else final_words
        seg = self.seg if self.seg is not None else self.next_seg()
        for i, sentence in enumerate(split_sentences(rest)):
            actions.append(Action("final", seg if i == 0 else self.next_seg(), " ".join(sentence)))
        self.reset()
        return actions

    def flush(self) -> list[Action]:
        """La conexión se cerró sin FINAL: cerramos lo que había en pantalla."""
        actions: list[Action] = []
        if self.prev and self.seg is not None:
            rest = self.prev[self._start_in(self.prev):]
            if rest:
                actions.append(Action("final", self.seg, " ".join(rest)))
        self.reset()
        return actions
