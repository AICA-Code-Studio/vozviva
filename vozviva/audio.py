"""Utilidades de audio: PCM 16 kHz mono s16le, VAD por energía y WAV."""
from __future__ import annotations

import io
import math
import wave
from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
FRAME_BYTES = FRAME_SAMPLES * BYTES_PER_SAMPLE


def level_dbfs(pcm: bytes) -> float:
    """Nivel RMS en dBFS de un bloque PCM s16le."""
    if len(pcm) < 2:
        return -120.0
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    return 20.0 * math.log10(rms / 32768.0 + 1e-9)


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(BYTES_PER_SAMPLE)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


@dataclass
class Segment:
    pcm: bytes
    t0: float  # segundos desde el inicio del flujo
    t1: float

    @property
    def duration(self) -> float:
        return self.t1 - self.t0


class Segmenter:
    """Corta el flujo de audio en fragmentos de habla.

    VAD por energía con piso de ruido adaptativo. Pensado para audio de
    escenario (micrófono cercano, consola de sonido), no para ambientes
    ruidosos. Cuando un fragmento supera `max_segment_s` se corta en el punto
    de menor energía de la última ventana para no partir palabras.
    """

    def __init__(
        self,
        min_silence_ms: int = 450,
        max_segment_s: float = 8.0,
        min_segment_s: float = 0.5,
        threshold_db: float = 10.0,
        absolute_floor_db: float = -52.0,
        pre_roll_ms: int = 240,
        start_frames: int = 3,
    ) -> None:
        self.min_silence_frames = max(1, min_silence_ms // FRAME_MS)
        self.max_frames = int(max_segment_s * 1000 / FRAME_MS)
        self.min_frames = int(min_segment_s * 1000 / FRAME_MS)
        self.threshold_db = threshold_db
        self.absolute_floor_db = absolute_floor_db
        self.start_frames = start_frames
        self.pre_roll: deque[tuple[bytes, float]] = deque(maxlen=max(1, pre_roll_ms // FRAME_MS))
        self.noise_floor = -60.0
        self._rest = b""
        self._frame_index = 0
        self._in_speech = False
        self._speech_run = 0
        self._silence = 0
        self._frames: list[bytes] = []
        self._levels: list[float] = []
        self._start_index = 0
        self.last_level = -120.0
        self.speaking = False

    # -- API pública -------------------------------------------------------
    def feed(self, pcm: bytes) -> list[Segment]:
        data = self._rest + pcm
        out: list[Segment] = []
        n = len(data) // FRAME_BYTES
        for i in range(n):
            frame = data[i * FRAME_BYTES : (i + 1) * FRAME_BYTES]
            seg = self._process(frame)
            if seg:
                out.extend(seg)
        self._rest = data[n * FRAME_BYTES :]
        return out

    def flush(self) -> list[Segment]:
        if self._in_speech and len(self._frames) >= self.min_frames:
            seg = self._emit(len(self._frames))
            self._reset_speech()
            return [seg]
        self._reset_speech()
        return []

    # -- internos ----------------------------------------------------------
    def _t(self, frame_index: int) -> float:
        return frame_index * FRAME_MS / 1000.0

    def _process(self, frame: bytes) -> list[Segment] | None:
        idx = self._frame_index
        self._frame_index += 1
        level = level_dbfs(frame)
        self.last_level = level
        gate = max(self.noise_floor + self.threshold_db, self.absolute_floor_db)
        is_speech = level > gate

        # El piso de ruido baja rápido y sube lento; solo se actualiza fuera de la voz.
        if not self._in_speech or not is_speech:
            if level < self.noise_floor:
                self.noise_floor = 0.8 * self.noise_floor + 0.2 * level
            else:
                self.noise_floor = 0.998 * self.noise_floor + 0.002 * level

        if not self._in_speech:
            self.pre_roll.append((frame, level))
            self._speech_run = self._speech_run + 1 if is_speech else 0
            if self._speech_run >= self.start_frames:
                self._in_speech = True
                self.speaking = True
                self._frames = [f for f, _ in self.pre_roll]
                self._levels = [l for _, l in self.pre_roll]
                self._start_index = idx - len(self._frames) + 1
                self.pre_roll.clear()
                self._silence = 0
            return None

        self._frames.append(frame)
        self._levels.append(level)
        self._silence = 0 if is_speech else self._silence + 1
        self.speaking = self._silence < self.min_silence_frames

        if self._silence >= self.min_silence_frames:
            keep = len(self._frames) - self._silence + min(self._silence, 5)
            segs = []
            if keep >= self.min_frames:
                segs.append(self._emit(keep))
            self._reset_speech()
            return segs

        if len(self._frames) >= self.max_frames:
            window = min(len(self._levels) // 3, int(1500 / FRAME_MS))
            start = len(self._levels) - window
            cut = start + int(np.argmin(self._levels[start:])) + 1
            seg = self._emit(cut)
            self._frames = self._frames[cut:]
            self._levels = self._levels[cut:]
            self._start_index += cut
            return [seg]
        return None

    def _emit(self, n_frames: int) -> Segment:
        pcm = b"".join(self._frames[:n_frames])
        t0 = self._t(self._start_index)
        return Segment(pcm=pcm, t0=t0, t1=t0 + n_frames * FRAME_MS / 1000.0)

    def _reset_speech(self) -> None:
        self._in_speech = False
        self.speaking = False
        self._speech_run = 0
        self._silence = 0
        self._frames = []
        self._levels = []
