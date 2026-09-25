"""Worker de una sesión (un escenario): fuente de audio → reconocimiento →
traducción → publicación. Cada sesión es independiente: si una falla, las
demás siguen."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, AsyncIterator

from .agenda import Agenda
from .audio import BYTES_PER_SAMPLE, SAMPLE_RATE, level_dbfs
from .backends import make_backend
from .bus import Publisher
from .config import SessionConfig, Settings
from .metrics import METER, Latency
from .models import Caption, TranscriptEvent
from .sources import CasterSource, make_source
from .translate import Translator

log = logging.getLogger("vozviva.worker")
BYTES_PER_SEC = SAMPLE_RATE * BYTES_PER_SAMPLE


class SessionWorker:
    def __init__(
        self, cfg: SessionConfig, settings: Settings, publisher: Publisher,
        translator: Translator | None, agenda: Agenda | None = None,
    ) -> None:
        self.cfg = cfg
        self.settings = settings
        self.publisher = publisher
        self.translator = translator
        self.agenda = agenda
        self.source = make_source(cfg)
        self.backend: Any = None
        self.context: deque[tuple[str, str]] = deque(maxlen=3)
        self.translate_sem = asyncio.Semaphore(settings.max_inflight)
        self.state = "iniciando"
        self.level_db = -120.0
        self.audio_seconds = 0.0
        self.last_audio = 0.0
        self.last_caption = 0.0
        self.finals = 0
        self.errors = 0
        self.last_error: str | None = None
        # Latencias: fin de la frase hablada → subtítulo final, y final → traducción.
        self.lat_asr = Latency()
        self.lat_tr = Latency()
        self._words: deque[tuple[float, int]] = deque(maxlen=400)  # (hora, palabras) de cada final
        self._run_s = 0.0
        self._wall: deque[tuple[float, float]] = deque(maxlen=3000)  # (segundo de audio, hora de llegada)
        self._task: asyncio.Task[None] | None = None

    @property
    def caster(self) -> CasterSource | None:
        return self.source if isinstance(self.source, CasterSource) else None

    # -- agenda ---------------------------------------------------------------
    def glossary(self) -> list[str]:
        terms: list[str] = []
        talk = self.agenda.relevant(self.cfg.id) if self.agenda else None
        for t in [*(talk.terms() if talk else []), *self.settings.glossary]:
            if t not in terms:
                terms.append(t)
        return terms[:100]  # la documentación sugiere hasta ~100 términos para mejores resultados

    def talk_context(self) -> str:
        talk = self.agenda.current(self.cfg.id) if self.agenda else None
        return talk.context() if talk else ""

    # -- ciclo de vida -----------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name=f"worker-{self.cfg.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                self.state = "esperando audio"
                self.backend = make_backend(self.settings, self.cfg)
                self.backend.glossary_fn = self.glossary
                self.backend.context_fn = self.talk_context
                self._run_s = 0.0
                self._wall.clear()
                await self.backend.run(self._metered(self.source.chunks()), self.on_event)
                self.state = "detenido"
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"[:300]
                self.state = "error"
                log.exception("[%s] el backend falló; reintento en %.0f s", self.cfg.id, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _metered(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for c in chunks:
            lvl = level_dbfs(c)
            self.level_db = 0.8 * self.level_db + 0.2 * lvl if self.level_db > -119 else lvl
            dur = len(c) / BYTES_PER_SEC
            self.audio_seconds += dur
            self._run_s += dur
            now = time.time()
            self._wall.append((self._run_s, now))
            self.last_audio = now
            if self.state in ("esperando audio", "iniciando"):
                self.state = "en vivo"
            yield c

    def _wall_at(self, t: float) -> float | None:
        """Hora en que llegó el audio del segundo `t` de esta corrida."""
        found = None
        for s, w in reversed(self._wall):
            if s < t:
                break
            found = w
        return found

    # -- eventos del backend ---------------------------------------------------
    async def on_event(self, ev: TranscriptEvent) -> None:
        lang = ev.lang or self.cfg.source_lang
        chan_lang = lang if ev.channel == "orig" else self.cfg.channel_lang(ev.channel)
        await self._publish(ev.channel, ev.seg, ev.text, ev.final, ev.t0, ev.t1, chan_lang)
        if not ev.final or ev.channel != "orig":
            return
        if ev.revision:
            await self._on_revision(ev, lang)
            return
        self.finals += 1
        self._words.append((time.time(), len(ev.text.split())))
        if getattr(self.backend, "measures_latency", False):
            w = self._wall_at(ev.t1)
            if w is not None:
                self.lat_asr.add(time.time() - w)
        for ch, txt in ev.translations.items():
            if ch in self.cfg.channels and txt:
                await self._publish(ch, ev.seg, txt, True, ev.t0, ev.t1, self.cfg.channel_lang(ch))
        provided = self.backend.provided_channels() if self.backend else set()
        missing = [c for c in self.cfg.channels if c != "orig" and c not in ev.translations and c not in provided]
        if missing and self.translator is not None:
            asyncio.create_task(self._translate(ev, lang, missing))
        else:
            self.context.append((ev.text, ""))

    async def _on_revision(self, ev: TranscriptEvent, lang: str) -> None:
        """El modelo corrigió una oración ya publicada: se retraduce solo si cambiaron las palabras."""
        from .stabilizer import norm_text

        if ev.previous is not None and norm_text(ev.previous) == norm_text(ev.text):
            return  # solo cambió la puntuación: el original ya se republicó, la traducción sigue valiendo
        provided = self.backend.provided_channels() if self.backend else set()
        missing = [c for c in self.cfg.channels if c != "orig" and c not in provided]
        if missing and self.translator is not None:
            asyncio.create_task(self._translate(ev, lang, missing, measure=False))

    async def _translate(self, ev: TranscriptEvent, lang: str, targets: list[str], measure: bool = True) -> None:
        assert self.translator is not None
        started = time.time()
        ctx = list(self.context)
        self.context.append((ev.text, ""))
        # Si el idioma detectado coincide con un destino, no hace falta traducir ese canal.
        same = [t for t in targets if t == lang]
        todo = [t for t in targets if t != lang]
        for t in same:
            await self._publish(t, ev.seg, ev.text, True, ev.t0, ev.t1, t)
        if not todo:
            return
        try:
            async with self.translate_sem:
                out = await self.translator.translate(
                    ev.text, lang, todo, ctx, glossary=self.glossary(), talk=self.talk_context(), session=self.cfg.id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.errors += 1
            self.last_error = f"traducción: {type(e).__name__}: {e}"[:300]
            log.warning("[%s] traducción falló: %s", self.cfg.id, e)
            return
        if measure:
            self.lat_tr.add(time.time() - started)
        for ch, txt in out.items():
            if ch in todo and txt:
                await self._publish(ch, ev.seg, txt, True, ev.t0, ev.t1, self.cfg.channel_lang(ch))

    async def _publish(self, channel: str, seg: int, text: str, final: bool, t0: float, t1: float, lang: str) -> None:
        self.last_caption = time.time()
        await self.publisher.publish(Caption(
            session=self.cfg.id, channel=channel, seg=seg, text=text, final=final, t0=round(t0, 2), t1=round(t1, 2), lang=lang,
        ))

    def pace(self, window_s: float = 45.0) -> dict[str, Any]:
        """Ritmo del orador: palabras por minuto en la ventana reciente y demora de los subtítulos."""
        s = self.settings
        now = time.time()
        recent = [(t, n) for t, n in self._words if now - t <= window_s]
        wpm = None
        if recent and now - recent[0][0] >= 15:  # con menos de 15 s de datos no medimos
            span = max(now - recent[0][0], 20.0)
            wpm = round(sum(n for _, n in recent) * 60 / span)
        lag_vals = list(self.lat_asr.values)[-5:]
        lag = round(sorted(lag_vals)[len(lag_vals) // 2], 1) if lag_vals else None
        if not self.last_caption or now - self.last_caption > 20:
            state = "idle"
        elif (wpm or 0) >= s.pace_wpm_alert or (lag or 0) >= s.pace_lag_alert_s:
            state = "alert"
        elif (wpm or 0) >= s.pace_wpm_warn or (lag or 0) >= s.pace_lag_warn_s:
            state = "warn"
        else:
            state = "ok"
        return {"state": state, "wpm": wpm, "lag_s": lag}

    def status(self) -> dict[str, Any]:
        now = time.time()
        src = self.source
        return {
            "id": self.cfg.id,
            "state": self.state,
            "backend": self.settings.backend,
            "input": "caster" if self.caster else "ffmpeg",
            "source_connected": getattr(src, "connected", False),
            "source_error": getattr(src, "last_error", None),
            "level_db": round(self.level_db, 1),
            "audio_minutes": round(self.audio_seconds / 60, 1),
            "seconds_since_audio": round(now - self.last_audio, 1) if self.last_audio else None,
            "seconds_since_caption": round(now - self.last_caption, 1) if self.last_caption else None,
            "finals": self.finals,
            "errors": self.errors,
            "last_error": self.last_error,
            "latency": {"speech_to_text": self.lat_asr.summary(), "translation": self.lat_tr.summary()},
            "usage": METER.report(self.cfg.id, self.settings.prices),
            "glossary_size": len(self.glossary()),
            "pace": self.pace(),
            "backend_status": getattr(self.backend, "status", {}),
        }
