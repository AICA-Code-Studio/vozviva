import asyncio

import numpy as np

from vozviva.assembler import SentenceAssembler
from vozviva.audio import SAMPLE_RATE, Segmenter
from vozviva.backends.gemini_chunked import parse_response
from vozviva.config import compute_channels
from vozviva.export import to_srt, to_vtt
from vozviva.models import Caption


def tone(seconds: float, amp: float = 0.3, noise: float = 0.0) -> bytes:
    n = int(SAMPLE_RATE * seconds)
    t = np.arange(n) / SAMPLE_RATE
    x = amp * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
    x += noise * np.random.default_rng(0).standard_normal(n)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def silence(seconds: float, noise: float = 0.002) -> bytes:
    return tone(seconds, amp=0.0, noise=noise)


def feed_all(seg: Segmenter, pcm: bytes, chunk: int = 3200):
    out = []
    for i in range(0, len(pcm), chunk):
        out += seg.feed(pcm[i:i + chunk])
    return out


def test_segmenter_splits_on_pauses():
    seg = Segmenter(min_silence_ms=450)
    pcm = silence(1.0) + tone(1.5) + silence(0.8) + tone(2.0) + silence(1.0)
    segs = feed_all(seg, pcm)
    assert len(segs) == 2
    assert 0.7 < segs[0].t0 < 1.05  # incluye ~240 ms de pre-roll
    assert 1.4 < segs[0].duration < 2.0
    assert 3.0 < segs[1].t0 < 3.35


def test_segmenter_caps_long_speech():
    seg = Segmenter(max_segment_s=4.0)
    segs = feed_all(seg, silence(0.5) + tone(10.0) + silence(1.0))
    assert len(segs) >= 3
    assert all(s.duration <= 4.01 for s in segs)
    total = sum(s.duration for s in segs)
    assert 9.5 < total < 10.8


def test_segmenter_ignores_silence():
    assert feed_all(Segmenter(), silence(5.0)) == []


def test_channels():
    assert compute_channels("en", ["es"], "en") == ["orig", "es"]
    assert compute_channels("es", ["es"], "en") == ["orig", "en"]
    assert compute_channels("auto", ["es"], "en") == ["orig", "es", "en"]
    assert compute_channels("pt", ["es", "en"], "en") == ["orig", "es", "en"]


def test_assembler_sentences_and_partials():
    events = []
    counter = iter(range(1, 100))

    async def emit(ev):
        events.append(ev)

    async def run():
        a = SentenceAssembler(emit, lambda: next(counter), max_chars=60)
        await a.add("Hola a", 0.0)
        await a.add(" todos. Hoy vamos", 1.0)
        await a.add(" a hablar de", 2.0)
        await a.flush()

    asyncio.run(run())
    finals = [e for e in events if e.final]
    assert [f.text for f in finals] == ["Hola a todos.", "Hoy vamos a hablar de"]
    assert finals[0].seg != finals[1].seg
    assert any(not e.final and e.text == "Hola a" for e in events)


def test_assembler_max_chars():
    events = []
    counter = iter(range(1, 100))

    async def emit(ev):
        events.append(ev)

    async def run():
        a = SentenceAssembler(emit, lambda: next(counter), max_chars=30)
        await a.add("uno dos tres cuatro cinco seis siete ocho nueve diez once", 0.0)
        await a.flush()

    asyncio.run(run())
    finals = [e.text for e in events if e.final]
    assert len(finals) == 2 and all(len(f) <= 31 for f in finals)


def test_parse_response():
    t, lang, tr = parse_response('```json\n{"transcript":"Hello","lang":"en","translations":{"es":"Hola"}}\n```', ["es"])
    assert (t, lang, tr) == ("Hello", "en", {"es": "Hola"})


def test_exports():
    caps = [
        Caption("s", "es", 2, "segundo", True, 3.0, 5.0, "es"),
        Caption("s", "es", 1, "primero", True, 0.5, 3.4, "es"),
        Caption("s", "es", 1, "primero (corregido)", True, 0.5, 3.4, "es"),
        Caption("s", "es", 3, "parcial", False, 6.0, 7.0, "es"),
    ]
    srt = to_srt(caps)
    assert srt.startswith("1\n00:00:00,500 --> 00:00:03,000\nprimero (corregido)")
    assert "parcial" not in srt
    assert to_vtt(caps).startswith("WEBVTT")
