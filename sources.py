"""Fuentes de audio. Todas entregan PCM s16le, 16 kHz, mono, en bloques de ~100 ms."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from .config import SessionConfig

log = logging.getLogger("vozviva.sources")

CHUNK_BYTES = 3200  # 100 ms


class FFmpegSource:
    """Lee cualquier entrada que entienda ffmpeg (SRT, RTMP, HLS, RTSP, placa de
    audio, archivo) y la convierte a PCM. Si ffmpeg se cae, lo reinicia con
    espera exponencial: en un evento en vivo el origen se corta y vuelve."""

    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg
        self.connected = False
        self.last_error: str | None = None

    def _args(self) -> list[str]:
        a = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.cfg.realtime:
            a += ["-re"]
        if self.cfg.loop:
            a += ["-stream_loop", "-1"]
        if self.cfg.input.startswith(("http://", "https://")):
            a += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if self.cfg.input_format:
            a += ["-f", self.cfg.input_format]
        a += ["-i", self.cfg.input, "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"]
        return a

    async def chunks(self) -> AsyncIterator[bytes]:
        backoff = 1.0
        while True:
            proc = await asyncio.create_subprocess_exec(
                *self._args(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            err_task = asyncio.create_task(self._drain_stderr(proc))
            try:
                assert proc.stdout is not None
                while True:
                    data = await proc.stdout.read(CHUNK_BYTES)
                    if not data:
                        break
                    self.connected = True
                    backoff = 1.0
                    yield data
            finally:
                self.connected = False
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                err_task.cancel()
            log.warning("[%s] ffmpeg terminó (código %s): %s", self.cfg.id, proc.returncode, self.last_error)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.last_error = line.decode(errors="replace").strip()[:300]


class CasterSource:
    """Audio que llega por WebSocket desde la página /caster (una notebook en la
    consola de sonido, el celular de un voluntario, etc.)."""

    def __init__(self, cfg: SessionConfig) -> None:
        self.cfg = cfg
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=600)  # ~60 s de colchón
        self.connected = False
        self.last_error: str | None = None
        self._owner: object | None = None

    def attach(self, owner: object) -> None:
        """El último caster que se conecta toma el control."""
        self._owner = owner
        self.connected = True

    def detach(self, owner: object) -> None:
        if self._owner is owner:
            self._owner = None
            self.connected = False

    def is_owner(self, owner: object) -> bool:
        return self._owner is owner

    def push(self, pcm: bytes) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()  # descartamos lo más viejo: preferimos latencia baja
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(pcm)

    async def chunks(self) -> AsyncIterator[bytes]:
        while True:
            yield await self.queue.get()


def make_source(cfg: SessionConfig) -> FFmpegSource | CasterSource:
    return CasterSource(cfg) if cfg.input == "caster" else FFmpegSource(cfg)
