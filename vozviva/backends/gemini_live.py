"""Backends sobre la Gemini Live API (WebSocket bidireccional).

- `gemini_live`: usa `gemini-3.5-transcribe-live`, un modelo dedicado de
  speech-to-text en streaming con parciales (interim) y finales. La traducción
  de cada final la hace el Translator (modelo de texto) en el worker.
- `gemini_live_translate`: usa `gemini-3.5-live-translate-preview`, que
  interpreta en simultáneo. Abre una conexión por idioma destino y toma la
  transcripción de entrada (original) y la de salida (traducción). El audio
  traducido que devuelve el modelo se descarta (queda para una versión futura).

Las sesiones de transcripción en vivo tienen duración máxima (10 min según la
documentación al 2026-09), así que las rotamos antes, en un silencio, sin
perder audio: mientras se abre la conexión nueva, el audio queda en cola.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..assembler import SentenceAssembler
from ..audio import BYTES_PER_SAMPLE, SAMPLE_RATE, Segmenter
from ..metrics import METER
from ..stabilizer import Action, UtteranceStabilizer
from ..models import TranscriptEvent
from .base import Backend, Emit

log = logging.getLogger("vozviva.gemini_live")
BYTES_PER_SEC = SAMPLE_RATE * BYTES_PER_SAMPLE
MIME = f"audio/pcm;rate={SAMPLE_RATE}"


def _short_lang(code: str | None) -> str | None:
    return code.split("-")[0].lower() if code else None


@dataclass
class _Ctx:
    channel: str
    seg: int | None = None
    t0: float = 0.0
    final_buf: str = ""
    interim: str = ""
    lang: str | None = None
    stab: UtteranceStabilizer | None = None
    seg_t0: dict[int, float] = field(default_factory=dict)
    # Para cada segmento en pantalla, cuándo apareció por primera vez cada palabra del parcial.
    word_times: dict[int, list[float]] = field(default_factory=dict)
    rotate: bool = False
    assemblers: dict[str, SentenceAssembler] = field(default_factory=dict)


class _LiveBase(Backend):
    model: str = ""

    def __init__(self, settings, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, session)
        from google import genai  # import diferido: solo si se usa este backend
        from google.genai import types

        if not settings.gemini_api_key:
            raise RuntimeError("Falta GEMINI_API_KEY para usar la Live API")
        self.types = types
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.t = 0.0  # segundos de audio recibidos en esta sesión
        self.speech_end_t = 0.0  # último fin de habla detectado por el VAD local
        self.emit: Emit | None = None
        self.status.update({"links": {}})

    # -- a implementar -----------------------------------------------------
    def link_channels(self) -> list[str]:
        raise NotImplementedError

    def config(self, channel: str) -> Any:
        raise NotImplementedError

    async def handle(self, msg: Any, ctx: _Ctx) -> None:
        raise NotImplementedError

    async def tick(self, ctx: _Ctx) -> None:
        return None

    async def close_ctx(self, ctx: _Ctx) -> None:
        return None

    # -- orquestación -----------------------------------------------------
    async def run(self, audio: AsyncIterator[bytes], emit: Emit) -> None:
        self.emit = emit
        queues = {c: asyncio.Queue(maxsize=600) for c in self.link_channels()}
        tasks = [asyncio.create_task(self._link(c, q), name=f"live-{self.session.id}-{c}") for c, q in queues.items()]
        try:
            async for chunk in audio:
                self.t += len(chunk) / BYTES_PER_SEC
                for q in queues.values():
                    if q.full():
                        q.get_nowait()
                    q.put_nowait(chunk)
        finally:
            for t in tasks:
                t.cancel()

    async def _link(self, channel: str, q: asyncio.Queue[bytes]) -> None:
        backoff = 1.0
        st: dict[str, Any] = {"connected": False, "reconnects": 0, "last_error": None}
        self.status["links"][channel] = st  # type: ignore[index]
        while True:
            first = await q.get()
            try:
                await self._session(channel, q, first, st)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                st["last_error"] = f"{type(e).__name__}: {e}"[:300]
                log.warning("[%s/%s] sesión Live terminó con error: %s", self.session.id, channel, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                st["connected"] = False
                st["reconnects"] += 1

    async def _session(self, channel: str, q: asyncio.Queue[bytes], first: bytes, st: dict[str, Any]) -> None:
        types = self.types
        ctx = _Ctx(channel=channel)
        vad = Segmenter(min_silence_ms=self.settings.min_silence_ms)
        started = time.monotonic()
        vocab = tuple(self.glossary_fn())
        vocab_checked = started
        vocab_changed = False
        async with self.client.aio.live.connect(model=self.model, config=self.config(channel)) as session:
            st["connected"] = True
            st["last_error"] = None
            recv = asyncio.create_task(self._receive(session, ctx))
            ticker = asyncio.create_task(self._ticker(ctx))
            chunk: bytes | None = first
            idle = 0.0
            was_speaking = False
            try:
                while True:
                    if chunk is None:
                        try:
                            chunk = await asyncio.wait_for(q.get(), timeout=1.0)
                            idle = 0.0
                        except asyncio.TimeoutError:
                            idle += 1.0
                    if recv.done():
                        recv.result()  # propaga el error de recepción
                        return
                    if chunk is not None:
                        await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type=MIME))
                        vad.feed(chunk)
                        chunk = None
                    if was_speaking and not vad.speaking:
                        self.speech_end_t = max(0.0, self.t - self.settings.min_silence_ms / 1000)
                        if self.settings.hybrid_vad:
                            await session.send_realtime_input(audio_stream_end=True)
                    was_speaking = vad.speaking
                    now = time.monotonic()
                    age = now - started
                    if now - vocab_checked > 10:  # cambió la charla: el glosario nuevo entra al reconectar
                        vocab_checked = now
                        vocab_changed = tuple(self.glossary_fn()) != vocab
                    rotate_at = self.settings.live_rotate_s
                    quiet = not vad.speaking
                    if ctx.rotate or idle > 20 or (quiet and (age > rotate_at or vocab_changed)) or age > rotate_at + 45:
                        log.info("[%s/%s] rotando sesión Live (%.0f s)", self.session.id, channel, age)
                        return
            finally:
                try:
                    await session.send_realtime_input(audio_stream_end=True)
                    for _ in range(25):  # hasta 2,5 s para recibir el último final
                        if ctx.seg is None and not ctx.final_buf:
                            break
                        await asyncio.sleep(0.1)
                except Exception:  # noqa: BLE001
                    pass
                recv.cancel()
                ticker.cancel()
                await self.close_ctx(ctx)

    async def _receive(self, session: Any, ctx: _Ctx) -> None:
        while True:
            async for msg in session.receive():
                if msg.go_away is not None:
                    ctx.rotate = True
                if msg.usage_metadata is not None:
                    METER.add(self.session.id, self.model, msg.usage_metadata)
                await self.handle(msg, ctx)

    async def _ticker(self, ctx: _Ctx) -> None:
        while True:
            await asyncio.sleep(0.3)
            await self.tick(ctx)


class GeminiLiveBackend(_LiveBase):
    """Transcripción en streaming con gemini-3.5-transcribe-live."""

    translates = False

    def __init__(self, settings, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, session)
        self.model = settings.model_live

    def link_channels(self) -> list[str]:
        return ["orig"]

    def config(self, channel: str) -> Any:
        t = self.types
        asr = t.AudioTranscriptionConfig(
            language_codes=self.session.asr_language_codes(),
            custom_vocabulary=self.glossary_fn()[:1000] or None,
            mode=self.settings.transcription_mode or None,
        )
        return t.LiveConnectConfig(response_modalities=[t.Modality.TEXT], input_audio_transcription=asr)

    async def handle(self, msg: Any, ctx: _Ctx) -> None:
        sc = msg.server_content
        if sc is None or self.emit is None:
            return
        st = self._stab(ctx)
        interim = sc.interim_input_transcription
        if interim is not None and interim.text:
            ctx.lang = _short_lang(interim.language_code) or ctx.lang
            await self._apply(ctx, st.interim(interim.text))
        final = sc.input_transcription
        if final is not None and final.text is not None:
            ctx.lang = _short_lang(final.language_code) or ctx.lang
            ctx.final_buf += final.text
            if final.finished is False:  # llegó un pedazo: esperamos el resto
                return
            text, ctx.final_buf = ctx.final_buf, ""
            await self._apply(ctx, st.final(text), from_model_final=True)
        if sc.turn_complete and ctx.final_buf:
            text, ctx.final_buf = ctx.final_buf, ""
            await self._apply(ctx, st.final(text), from_model_final=True)
        ctx.seg = st.seg  # para saber si queda texto pendiente al rotar la conexión

    def _stab(self, ctx: _Ctx) -> UtteranceStabilizer:
        if ctx.stab is None:
            ctx.stab = UtteranceStabilizer(self.next_seg)
        return ctx.stab

    async def _apply(self, ctx: _Ctx, actions: list[Action], from_model_final: bool = False) -> None:
        assert self.emit is not None
        lang = ctx.lang or self.session.source_lang
        for a in actions:
            if a.seg not in ctx.seg_t0:
                ctx.seg_t0[a.seg] = self.t
            t0 = ctx.seg_t0[a.seg]
            if a.kind == "partial":
                times = ctx.word_times.setdefault(a.seg, [])
                n = len(a.text.split())
                times.extend([self.t] * max(0, n - len(times)))
                await self.emit(TranscriptEvent(seg=a.seg, text=a.text, final=False, t0=t0, t1=self.t, lang=lang))
                continue
            if from_model_final and not a.revision:
                # Fin de la frase según el VAD local, si cae dentro del segmento.
                t1 = self.speech_end_t if t0 <= self.speech_end_t <= self.t else self.t
            else:
                # Oración cerrada por el estabilizador: cuándo se vio su última palabra.
                times = ctx.word_times.get(a.seg, [])
                k = len(a.text.split())
                t1 = times[k - 1] if 0 < k <= len(times) else self.t
            await self.emit(TranscriptEvent(
                seg=a.seg, text=a.text, final=True, t0=t0, t1=t1, lang=lang,
                revision=a.revision, previous=a.previous,
            ))
            if not a.revision:
                ctx.seg_t0.pop(a.seg, None)
                ctx.word_times.pop(a.seg, None)

    async def close_ctx(self, ctx: _Ctx) -> None:
        # La conexión se cerró: si llegó un final a medias o quedó un parcial en pantalla, lo cerramos.
        st = self._stab(ctx)
        if ctx.final_buf:
            text, ctx.final_buf = ctx.final_buf, ""
            await self._apply(ctx, st.final(text), from_model_final=True)
        else:
            await self._apply(ctx, st.flush())
        ctx.seg = None


class GeminiLiveTranslateBackend(_LiveBase):
    """Interpretación simultánea con gemini-3.5-live-translate-preview (experimental)."""

    translates = True

    def __init__(self, settings, session) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings, session)
        self.model = settings.model_live_translate
        self._orig_channel = self.link_channels()[0]

    def link_channels(self) -> list[str]:
        # El canal de lectura fácil no es un idioma: lo resuelve el traductor del worker.
        return [c for c in self.session.channels if c not in ("orig", "facil")]

    def provided_channels(self) -> set[str]:
        return set(self.link_channels())

    def config(self, channel: str) -> Any:
        t = self.types
        return t.LiveConnectConfig(
            response_modalities=[t.Modality.AUDIO],
            input_audio_transcription=t.AudioTranscriptionConfig(),
            output_audio_transcription=t.AudioTranscriptionConfig(),
            translation_config=t.TranslationConfig(target_language_code=channel, echo_target_language=False),
        )

    def _assembler(self, ctx: _Ctx, channel: str, lang: str | None) -> SentenceAssembler:
        if channel not in ctx.assemblers:
            assert self.emit is not None
            ctx.assemblers[channel] = SentenceAssembler(self.emit, self.next_seg, channel=channel, lang=lang)
        return ctx.assemblers[channel]

    async def handle(self, msg: Any, ctx: _Ctx) -> None:
        sc = msg.server_content
        if sc is None:
            return
        if sc.input_transcription is not None and sc.input_transcription.text and ctx.channel == self._orig_channel:
            lang = _short_lang(sc.input_transcription.language_code) or self.session.source_lang
            await self._assembler(ctx, "orig", lang).add(sc.input_transcription.text, self.t)
        if sc.output_transcription is not None and sc.output_transcription.text:
            await self._assembler(ctx, ctx.channel, ctx.channel).add(sc.output_transcription.text, self.t)
        if sc.turn_complete:
            for a in ctx.assemblers.values():
                await a.flush()

    async def tick(self, ctx: _Ctx) -> None:
        for a in ctx.assemblers.values():
            await a.tick()

    async def close_ctx(self, ctx: _Ctx) -> None:
        for a in ctx.assemblers.values():
            await a.flush()
