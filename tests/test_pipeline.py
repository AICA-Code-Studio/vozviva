"""Prueba el flujo completo con clientes falsos de Gemini (sin red)."""
import asyncio

from google.genai import types

from vozviva.backends.gemini_live import GeminiLiveBackend
from vozviva.bus import FileStore, Hub, Publisher
from vozviva.config import SessionConfig, Settings, compute_channels
from vozviva.sources import FFmpegSource
from vozviva.worker import SessionWorker


def msg(**sc):
    return types.LiveServerMessage(server_content=types.LiveServerContent(**sc))


class FakeSession:
    def __init__(self, script):
        self.script = script
        self.sent = 0

    async def send_realtime_input(self, **kw):
        if kw.get("audio") is not None:
            self.sent += 1

    async def receive(self):
        while self.script:
            m = self.script.pop(0)
            await asyncio.sleep(0.01)
            yield m
            if m.server_content and m.server_content.turn_complete:
                return
        await asyncio.sleep(3600)


class FakeConnect:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


class FakeTranslator:
    def __init__(self):
        self.calls = []

    async def translate(self, text, src, targets, context, glossary=None, talk="", session=""):
        self.calls.append({"targets": targets, "glossary": glossary, "talk": talk, "session": session})
        return {t: f"[{t}] {text}" for t in targets}


def test_live_backend_to_audience(tmp_path):
    settings = Settings(backend="gemini_live", gemini_api_key="x", data_dir=str(tmp_path))
    sc = SessionConfig(id="sala", name="Sala", source_lang="en", input="caster")
    sc.channels = compute_channels("en", ["es"], "en")
    settings.sessions = [sc]
    script = [
        msg(interim_input_transcription=types.Transcription(text="Hello every")),
        msg(interim_input_transcription=types.Transcription(text="Hello everyone")),
        msg(input_transcription=types.Transcription(text="Hello everyone.", language_code="en-US")),
        msg(interim_input_transcription=types.Transcription(text="Today")),
        msg(input_transcription=types.Transcription(text="Today we talk about queues.")),
        msg(turn_complete=True),
    ]
    fake = FakeSession(script)

    async def run():
        hub = Hub()
        pub = Publisher(hub, FileStore(str(tmp_path)))
        tr = FakeTranslator()
        w = SessionWorker(sc, settings, pub, tr)
        q_orig = hub.subscribe(("sala", "orig"))
        q_es = hub.subscribe(("sala", "es"))
        import vozviva.worker as wm

        real = wm.make_backend

        def patched(s, c):
            b = GeminiLiveBackend(s, c)
            b.client.aio.live.connect = lambda model, config: FakeConnect(fake)
            return b

        wm.make_backend = patched
        try:
            w.start()
            for _ in range(10):
                w.caster.push(b"\x00\x10" * 1600)
            await asyncio.sleep(1.0)
        finally:
            wm.make_backend = real
            await w.stop()
        orig = [q_orig.get_nowait() for _ in range(q_orig.qsize())]
        es = [q_es.get_nowait() for _ in range(q_es.qsize())]
        return orig, es, fake, tr, w.status()

    orig, es, fake, tr, status = asyncio.run(run())
    assert tr.calls[0]["session"] == "sala" and tr.calls[0]["glossary"] is not None
    assert status["latency"]["translation"]["n"] == 2
    assert status["latency"]["speech_to_text"]["n"] == 2
    finals = [c for c in orig if c.final]
    assert [c.text for c in finals] == ["Hello everyone.", "Today we talk about queues."]
    assert finals[0].lang == "en"
    assert any(not c.final and c.text == "Hello everyone" for c in orig)
    assert sorted(c.text for c in es) == ["[es] Hello everyone.", "[es] Today we talk about queues."]
    assert {c.seg for c in es} == {c.seg for c in finals}
    assert fake.sent >= 1
    assert (tmp_path / "transcripts" / "sala.jsonl").exists()


def test_ffmpeg_source():
    sc = SessionConfig(id="t", name="t", input="sine=frequency=300:duration=1", input_format="lavfi")

    async def run():
        src = FFmpegSource(sc)
        total = 0
        async for chunk in src.chunks():
            total += len(chunk)
            if total >= 32000:
                break
        return total

    assert asyncio.run(asyncio.wait_for(run(), 10)) >= 32000


def test_chunked_backend_with_fake_client():
    import numpy as np

    from vozviva.backends.gemini_chunked import GeminiChunkedBackend

    settings = Settings(backend="gemini_chunked", gemini_api_key="x")
    sc = SessionConfig(id="s", name="s", source_lang="en")
    sc.channels = compute_channels("en", ["es"], "en")
    b = GeminiChunkedBackend(settings, sc)
    calls = []

    class R:
        text = '{"transcript": "Hi there.", "lang": "en", "translations": {"es": "Hola."}}'

    async def fake_generate(model, contents, config):
        calls.append((model, contents, config))
        return R()

    b.client.aio.models.generate_content = fake_generate
    t = np.arange(16000 * 2) / 16000
    speech = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16).tobytes()
    pcm = b"\x00\x00" * 8000 + speech + b"\x00\x00" * 16000

    async def audio():
        for i in range(0, len(pcm), 3200):
            yield pcm[i:i + 3200]
        await asyncio.sleep(0.3)

    events = []

    async def emit(ev):
        events.append(ev)

    asyncio.run(b.run(audio(), emit))
    assert len(calls) == 1 and calls[0][0] == "gemini-3.5-flash-lite"
    assert calls[0][2].response_schema is not None
    assert events[0].text == "Hi there." and events[0].translations == {"es": "Hola."}


def test_live_backend_real_like_stream(tmp_path):
    """Parciales acumulados reales y un único FINAL al minuto: las oraciones se publican y traducen antes."""
    from tests.test_stabilizer import FINAL, interims

    settings = Settings(backend="gemini_live", gemini_api_key="x", data_dir=str(tmp_path))
    sc = SessionConfig(id="sala", name="Sala", source_lang="en", input="caster")
    sc.channels = compute_channels("en", ["es"], "en")
    settings.sessions = [sc]
    script = [msg(interim_input_transcription=types.Transcription(text=t)) for t in interims()]
    script.append(msg(input_transcription=types.Transcription(text=FINAL)))
    script.append(msg(turn_complete=True))
    fake = FakeSession(script)

    async def run():
        hub = Hub()
        pub = Publisher(hub, FileStore(str(tmp_path)))
        tr = FakeTranslator()
        w = SessionWorker(sc, settings, pub, tr)
        q_orig, q_es = hub.subscribe(("sala", "orig")), hub.subscribe(("sala", "es"))
        import vozviva.worker as wm

        real = wm.make_backend

        def patched(s, c):
            b = GeminiLiveBackend(s, c)
            b.client.aio.live.connect = lambda model, config: FakeConnect(fake)
            return b

        wm.make_backend = patched
        try:
            w.start()
            w.caster.push(b"\x00\x10" * 1600)
            await asyncio.sleep(4.0)
        finally:
            wm.make_backend = real
            await w.stop()
        return [q_orig.get_nowait() for _ in range(q_orig.qsize())], [q_es.get_nowait() for _ in range(q_es.qsize())]

    orig, es = asyncio.run(run())
    finals = [c for c in orig if c.final]
    first_model_final = next(i for i, c in enumerate(orig) if c.final and "upfront" in c.text)
    assert sum(1 for c in orig[:first_model_final] if c.final) >= 8  # oraciones publicadas antes del FINAL del modelo
    by_seg = {}
    for c in finals:
        by_seg[c.seg] = c.text  # la corrección reemplaza al texto anterior (mismo seg)
    assert any("ARC AGI 3" in t for t in by_seg.values())
    assert len({c.seg for c in es}) >= 8  # cada oración se tradujo por separado
    assert any("ARC AGI 3" in c.text for c in es)  # la corrección se retradujo
