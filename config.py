"""Carga de configuración (YAML + variables de entorno)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LANG_NAMES = {
    "es": "Español",
    "en": "English",
    "pt": "Português",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "ja": "日本語",
    "zh": "中文",
}

# Canal especial: español en formato de Lectura Fácil.
EASY = "facil"
CHANNEL_LABELS = {EASY: "Español fácil"}

# Cómo se le describe cada destino al modelo que traduce.
_EASY_DESC = (
    "español en formato de Lectura Fácil: oraciones cortas con una sola idea, palabras de uso cotidiano, "
    "voz activa, sin metáforas ni jerga; si aparece un término técnico, mantenelo y explicalo en pocas palabras"
)


def target_desc(code: str) -> str:
    return _EASY_DESC if code == EASY else LANG_NAMES.get(code, code)


# Pistas BCP-47 para el reconocimiento en vivo (gemini-3.5-transcribe-live).
DEFAULT_BCP47 = {
    "es": "es-419",
    "en": "en-US",
    "pt": "pt-BR",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "ja": "ja-JP",
}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass
class SessionConfig:
    id: str
    name: str
    source_lang: str = "en"          # código ISO 639-1 o "auto"
    input: str = "caster"            # "caster" o cualquier entrada que entienda ffmpeg
    input_format: str | None = None  # p. ej. "alsa", "pulse", "avfoundation", "dshow"
    realtime: bool = False           # agrega -re (para simular vivo con un archivo)
    loop: bool = False               # repite el archivo (demos)
    description: str = ""
    language_hint: str | None = None  # BCP-47 para ASR; por defecto se deriva de source_lang
    channels: list[str] = field(default_factory=list)  # calculado: ["orig", "es", ...]

    def asr_language_codes(self) -> list[str]:
        if self.language_hint:
            return [self.language_hint]
        if self.source_lang == "auto":
            return []
        return [DEFAULT_BCP47.get(self.source_lang, self.source_lang)]

    def channel_lang(self, channel: str) -> str:
        if channel == "orig":
            return self.source_lang
        return "es" if channel == EASY else channel

    def public(self) -> dict[str, Any]:
        chans = []
        for c in self.channels:
            lang = self.channel_lang(c)
            label = CHANNEL_LABELS.get(c) or LANG_NAMES.get(lang, lang)
            short = "Fácil" if c == EASY else label
            if c == "orig":
                label = f"Original ({label})" if lang != "auto" else "Original"
                short = "Original"
            chans.append({"id": c, "lang": lang, "label": label, "short": short})
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source_lang": self.source_lang,
            "channels": chans,
        }


@dataclass
class Settings:
    backend: str = "gemini_live"      # gemini_live | gemini_live_translate | gemini_chunked | local | mock
    translator: str = "gemini"        # gemini | ollama | none
    targets: list[str] = field(default_factory=lambda: ["es", "pt"])
    reverse_target: str = "en"        # si la charla es en español, también se traduce a este idioma
    easy_read: bool = True            # agrega el canal "Español fácil" (Lectura Fácil)

    gemini_api_key: str | None = None
    model_live: str = "gemini-3.5-transcribe-live"
    model_live_translate: str = "gemini-3.5-live-translate-preview"
    model_chunked: str = "gemini-3.5-flash-lite"
    model_translate: str = "gemini-3.5-flash-lite"
    transcription_mode: str = "SMART"  # SMART | VERBATIM
    hybrid_vad: bool = False            # envía audio_stream_end al detectar silencio local
    live_rotate_s: float = 540.0        # las sesiones de transcripción en vivo duran hasta 10 min

    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "gemma3:4b"
    whisper_model: str = "small"
    whisper_device: str = "auto"

    glossary: list[str] = field(default_factory=list)
    caster_token: str | None = None
    redis_url: str | None = None
    data_dir: str = "data"
    history_size: int = 300
    max_inflight: int = 3
    min_silence_ms: int = 450
    max_segment_s: float = 8.0

    # Agenda del evento (charlas por sala y horario): glosario y contexto automáticos.
    agenda: str | None = None
    timezone: str = "America/Argentina/Buenos_Aires"
    # URL pública (para los QR de los carteles). Si falta, se usa la del pedido.
    public_url: str | None = None
    # Resúmenes "¿Qué me perdí?"
    model_summary: str | None = None  # por defecto, model_translate
    summary_cache_s: float = 30.0
    # Monitor de ritmo para el orador (/orador): umbrales de palabras por minuto y demora de subtítulos.
    pace_wpm_warn: int = 170
    pace_wpm_alert: int = 200
    pace_lag_warn_s: float = 4.0
    pace_lag_alert_s: float = 7.0

    # Precios en USD por millón de tokens, por modelo: {modelo: {input: x, output: y}}.
    prices: dict[str, dict[str, float]] = field(default_factory=dict)

    sessions: list[SessionConfig] = field(default_factory=list)

    def session(self, sid: str) -> SessionConfig | None:
        return next((s for s in self.sessions if s.id == sid), None)


def compute_channels(src: str, targets: list[str], reverse: str, easy_read: bool = False) -> list[str]:
    chans = ["orig"]
    wanted = list(targets)
    if src == "es" and reverse:
        wanted.insert(0, reverse)  # charla en español: primero el inglés, que es lo que pide el desafío
    elif src == "auto" and reverse:
        wanted.append(reverse)
    for t in wanted:
        if t != src and t not in chans:
            chans.append(t)
    if easy_read:
        chans.append(EASY)
    return chans


def _env_expand(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_env_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _env_expand(v) for k, v in value.items()}
    return value


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = _env_expand(raw)
    sessions_raw = raw.pop("sessions", [])
    known = set(Settings.__dataclass_fields__) - {"sessions"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"Claves desconocidas en la configuración: {sorted(unknown)}")
    s = Settings(**raw)

    s.gemini_api_key = s.gemini_api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    s.caster_token = s.caster_token or os.environ.get("VOZVIVA_CASTER_TOKEN")
    s.redis_url = s.redis_url or os.environ.get("VOZVIVA_REDIS_URL")

    seen: set[str] = set()
    for item in sessions_raw:
        sc = SessionConfig(**item)
        if not _SLUG.match(sc.id):
            raise ValueError(f"id de sesión inválido: {sc.id!r} (usar minúsculas, números, - o _)")
        if sc.id in seen:
            raise ValueError(f"id de sesión duplicado: {sc.id}")
        seen.add(sc.id)
        sc.channels = compute_channels(sc.source_lang, s.targets, s.reverse_target, s.easy_read)
        s.sessions.append(sc)
    if not s.sessions:
        raise ValueError("La configuración no define ninguna sesión")
    return s
