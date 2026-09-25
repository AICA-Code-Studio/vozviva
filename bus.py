"""Distribución de subtítulos a la audiencia.

Modo simple (un proceso): los workers publican en el Hub en memoria y cada
conexión SSE de la audiencia es una cola suscripta al Hub.

Modo distribuido (varios procesos o máquinas): los workers publican en Redis
y cada nodo web retransmite desde Redis a su Hub local. Así se pueden separar
los nodos que procesan audio de los que atienden a la audiencia.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from .models import Caption

log = logging.getLogger("vozviva.bus")
Key = tuple[str, str]  # (sesión, canal)

REDIS_CHANNEL = "vozviva:captions"
REDIS_STATUS = "vozviva:status"


class Hub:
    def __init__(self, history_size: int = 300) -> None:
        self.history_size = history_size
        self.history: dict[Key, deque[Caption]] = defaultdict(lambda: deque(maxlen=history_size))
        self.partials: dict[Key, Caption] = {}
        self.subs: dict[Key, set[asyncio.Queue[Caption]]] = defaultdict(set)
        self.dropped = 0

    def dispatch(self, cap: Caption) -> None:
        key = (cap.session, cap.channel)
        if cap.final:
            self.history[key].append(cap)
            p = self.partials.get(key)
            if p is not None and p.seg <= cap.seg:
                self.partials.pop(key, None)
        else:
            self.partials[key] = cap
        for q in list(self.subs.get(key, ())):
            try:
                q.put_nowait(cap)
            except asyncio.QueueFull:
                self.dropped += 1  # cliente lento: pierde parciales, se resincroniza al reconectar

    def subscribe(self, key: Key) -> asyncio.Queue[Caption]:
        q: asyncio.Queue[Caption] = asyncio.Queue(maxsize=500)
        self.subs[key].add(q)
        return q

    def unsubscribe(self, key: Key, q: asyncio.Queue[Caption]) -> None:
        self.subs[key].discard(q)

    def snapshot(self, key: Key) -> list[Caption]:
        items = list(self.history.get(key, ()))
        p = self.partials.get(key)
        if p is not None:
            items.append(p)
        return items

    def seed(self, key: Key, caps: list[Caption]) -> None:
        if key not in self.history or not self.history[key]:
            for c in caps[-self.history_size:]:
                self.history[key].append(c)

    def viewers(self, session: str) -> int:
        return sum(len(v) for (s, _), v in self.subs.items() if s == session)


class TranscriptStore:
    """Transcripción completa (solo finales), para exportar TXT/SRT/VTT."""

    async def append(self, cap: Caption) -> None: ...

    async def read(self, session: str, channel: str) -> list[Caption]: ...


class FileStore(TranscriptStore):
    def __init__(self, data_dir: str) -> None:
        self.dir = Path(data_dir) / "transcripts"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session: str) -> Path:
        return self.dir / f"{session}.jsonl"

    async def append(self, cap: Caption) -> None:
        line = cap.to_json() + "\n"
        await asyncio.to_thread(self._write, self._path(cap.session), line)

    @staticmethod
    def _write(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    async def read(self, session: str, channel: str) -> list[Caption]:
        path = self._path(session)
        if not path.exists():
            return []
        text = await asyncio.to_thread(path.read_text, "utf-8")
        out = []
        for line in text.splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("channel") == channel:
                out.append(Caption.from_dict(d))
        return out


class RedisStore(TranscriptStore):
    def __init__(self, redis: Any) -> None:
        self.r = redis

    async def append(self, cap: Caption) -> None:
        await self.r.rpush(f"vozviva:tx:{cap.session}:{cap.channel}", cap.to_json())

    async def read(self, session: str, channel: str) -> list[Caption]:
        raw = await self.r.lrange(f"vozviva:tx:{session}:{channel}", 0, -1)
        return [Caption.from_dict(json.loads(x)) for x in raw]


class Publisher:
    def __init__(self, hub: Hub, store: TranscriptStore, redis: Any | None = None) -> None:
        self.hub = hub
        self.store = store
        self.redis = redis

    async def publish(self, cap: Caption) -> None:
        if cap.final:
            try:
                await self.store.append(cap)
            except Exception as e:  # noqa: BLE001 - no cortar el vivo por un error de disco
                log.error("No se pudo guardar la transcripción: %s", e)
        if self.redis is not None:
            await self.redis.publish(REDIS_CHANNEL, cap.to_json())
        else:
            self.hub.dispatch(cap)


async def redis_relay(redis: Any, hub: Hub) -> None:
    """Nodo web: escucha Redis y reparte a las conexiones locales."""
    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    hub.dispatch(Caption.from_dict(json.loads(msg["data"])))
                except Exception as e:  # noqa: BLE001
                    log.warning("Mensaje inválido en Redis: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Relay de Redis caído, reintento: %s", e)
            await asyncio.sleep(2)
