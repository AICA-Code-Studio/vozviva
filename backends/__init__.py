"""Fábrica de backends de reconocimiento."""
from __future__ import annotations

from ..config import SessionConfig, Settings
from .base import Backend

BACKENDS = ("gemini_live", "gemini_live_translate", "gemini_chunked", "local", "mock")


def make_backend(settings: Settings, session: SessionConfig) -> Backend:
    name = settings.backend
    if name == "gemini_live":
        from .gemini_live import GeminiLiveBackend
        return GeminiLiveBackend(settings, session)
    if name == "gemini_live_translate":
        from .gemini_live import GeminiLiveTranslateBackend
        return GeminiLiveTranslateBackend(settings, session)
    if name == "gemini_chunked":
        from .gemini_chunked import GeminiChunkedBackend
        return GeminiChunkedBackend(settings, session)
    if name == "local":
        from .local import LocalBackend
        return LocalBackend(settings, session)
    if name == "mock":
        from .mock import MockBackend
        return MockBackend(settings, session)
    raise ValueError(f"Backend desconocido: {name!r}. Opciones: {', '.join(BACKENDS)}")
