"""Exportación de transcripciones: TXT, SRT, WebVTT."""
from __future__ import annotations

from .models import Caption


def _ts(sec: float, sep: str) -> str:
    ms = int(round(max(sec, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def dedupe(caps: list[Caption]) -> list[Caption]:
    """Un final por segmento (el último gana), ordenado por tiempo."""
    by_seg: dict[int, Caption] = {}
    for c in caps:
        if c.final:
            by_seg[c.seg] = c
    return sorted(by_seg.values(), key=lambda c: (c.t0, c.seg))


def _fix_times(caps: list[Caption]) -> list[tuple[float, float, str]]:
    out = []
    for i, c in enumerate(caps):
        t0, t1 = c.t0, max(c.t1, c.t0 + 1.0)
        if i + 1 < len(caps):
            t1 = min(t1, max(caps[i + 1].t0, t0 + 0.5))
        out.append((t0, t1, c.text))
    return out


def to_txt(caps: list[Caption]) -> str:
    return "\n".join(c.text for c in dedupe(caps)) + "\n"


def to_srt(caps: list[Caption]) -> str:
    blocks = []
    for i, (t0, t1, text) in enumerate(_fix_times(dedupe(caps)), 1):
        blocks.append(f"{i}\n{_ts(t0, ',')} --> {_ts(t1, ',')}\n{text}\n")
    return "\n".join(blocks)


def to_vtt(caps: list[Caption]) -> str:
    blocks = ["WEBVTT\n"]
    for t0, t1, text in _fix_times(dedupe(caps)):
        blocks.append(f"{_ts(t0, '.')} --> {_ts(t1, '.')}\n{text}\n")
    return "\n".join(blocks)
