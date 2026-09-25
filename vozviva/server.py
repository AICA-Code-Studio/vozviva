"""Servidor HTTP: vista de audiencia, API, ingesta desde navegador y estado."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from . import export
from .agenda import Agenda
from .bus import REDIS_STATUS, FileStore, Hub, Publisher, RedisStore, redis_relay
from .config import Settings
from .explain import Explainer
from .summarize import Summarizer
from .translate import make_translator
from .worker import SessionWorker

log = logging.getLogger("vozviva.server")
STATIC = Path(__file__).parent / "static"


class Runtime:
    def __init__(self, settings: Settings, run_sessions: list[str] | None) -> None:
        self.settings = settings
        self.hub = Hub(settings.history_size)
        self.redis: Any = None
        self.workers: dict[str, SessionWorker] = {}
        self.run_sessions = [s.id for s in settings.sessions] if run_sessions is None else run_sessions
        self._tasks: list[asyncio.Task[None]] = []
        self.agenda = Agenda(settings.agenda, settings.timezone, [s.id for s in settings.sessions])
        self.summarizer: Summarizer | None = None
        self.summary_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
        self.summary_locks: dict[tuple[str, str, int], asyncio.Lock] = {}
        self.explainer: Explainer | None = None
        self.explain_cache: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.explain_locks: dict[tuple[str, str, int], asyncio.Lock] = {}

    async def start(self) -> None:
        s = self.settings
        if s.redis_url:
            import redis.asyncio as aioredis

            self.redis = aioredis.from_url(s.redis_url, decode_responses=True)
            self.store = RedisStore(self.redis)
            self._tasks.append(asyncio.create_task(redis_relay(self.redis, self.hub)))
            self._tasks.append(asyncio.create_task(self._status_loop()))
        else:
            self.store = FileStore(s.data_dir)
        self.publisher = Publisher(self.hub, self.store, self.redis)

        translator = None
        needs_translator = s.backend in ("gemini_live", "local") or (s.backend == "gemini_live_translate" and s.easy_read)
        if needs_translator and s.translator != "none" and self.run_sessions:
            translator = make_translator(s)
        self.summarizer = Summarizer(s)
        self.explainer = Explainer(s)
        for sid in self.run_sessions:
            cfg = s.session(sid)
            if cfg is None:
                raise ValueError(f"--sessions menciona una sesión que no existe: {sid}")
            w = SessionWorker(cfg, s, self.publisher, translator, self.agenda)
            self.workers[sid] = w
            w.start()
        log.info("Vozviva listo: backend=%s, sesiones locales=%s, redis=%s", s.backend, self.run_sessions or "ninguna", bool(self.redis))

    async def stop(self) -> None:
        for w in self.workers.values():
            await w.stop()
        for t in self._tasks:
            t.cancel()
        if self.redis is not None:
            await self.redis.aclose()

    async def _status_loop(self) -> None:
        while True:
            try:
                if self.workers:
                    await self.redis.hset(REDIS_STATUS, mapping={sid: json.dumps(w.status()) for sid, w in self.workers.items()})
            except Exception as e:  # noqa: BLE001
                log.warning("No se pudo publicar el estado en Redis: %s", e)
            await asyncio.sleep(2)

    async def status(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if self.redis is not None:
            try:
                raw = await self.redis.hgetall(REDIS_STATUS)
                out.update({k: json.loads(v) for k, v in raw.items()})
            except Exception as e:  # noqa: BLE001
                log.warning("No se pudo leer el estado de Redis: %s", e)
        out.update({sid: w.status() for sid, w in self.workers.items()})
        for sc in self.settings.sessions:
            st = out.setdefault(sc.id, {"id": sc.id, "state": "sin worker"})
            st["viewers"] = self.hub.viewers(sc.id)
        return out


def create_app(settings: Settings, run_sessions: list[str] | None = None) -> FastAPI:
    rt = Runtime(settings, run_sessions)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await rt.start()
        yield
        await rt.stop()

    app = FastAPI(title="Vozviva", lifespan=lifespan)
    app.state.runtime = rt

    def page(name: str) -> FileResponse:
        return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def index() -> FileResponse:
        return page("index.html")

    @app.get("/caster")
    async def caster() -> FileResponse:
        return page("caster.html")

    @app.get("/overlay")
    async def overlay() -> FileResponse:
        return page("overlay.html")

    @app.get("/ops")
    async def ops() -> FileResponse:
        return page("ops.html")

    @app.get("/orador")
    async def orador() -> FileResponse:
        return page("orador.html")

    @app.get("/carteles")
    async def carteles() -> FileResponse:
        return page("carteles.html")

    @app.get("/api/agenda")
    async def agenda() -> JSONResponse:
        rt.agenda.maybe_reload()
        return JSONResponse({
            "loaded": bool(settings.agenda), "error": rt.agenda.error, "timezone": settings.timezone,
            "talks": [dict(t.public(), session=t.session) for t in rt.agenda.talks],
        })

    @app.get("/api/sessions/{sid}/qr.svg")
    async def qr(sid: str, request: Request, base: str | None = None) -> Response:
        import segno

        if settings.session(sid) is None:
            raise HTTPException(404, "Sesión inexistente")
        root = (base or settings.public_url or str(request.base_url)).rstrip("/")
        url = f"{root}/?s={sid}"
        import io

        buf = io.BytesIO()
        segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark="#000", light="#fff", xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-cache", "X-Vozviva-Url": url})

    @app.get("/static/{name}")
    async def static(name: str) -> FileResponse:
        path = (STATIC / name).resolve()
        if path.parent != STATIC.resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=300"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"ok": "true"}

    @app.get("/api/sessions")
    async def sessions() -> JSONResponse:
        st = await rt.status()
        items = []
        for sc in settings.sessions:
            d = sc.public()
            s = st.get(sc.id, {})
            since_audio = s.get("seconds_since_audio")
            since_cap = s.get("seconds_since_caption")
            d["live"] = (since_audio is not None and since_audio < 15) or (since_cap is not None and since_cap < 20)
            d["viewers"] = s.get("viewers", 0)
            cur, nxt = rt.agenda.current(sc.id), rt.agenda.next(sc.id)
            d["now"] = cur.public() if cur else None
            d["next"] = nxt.public() if nxt else None
            items.append(d)
        return JSONResponse(items, headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    async def status() -> JSONResponse:
        return JSONResponse(await rt.status(), headers={"Cache-Control": "no-cache"})

    def check(sid: str, channel: str) -> None:
        sc = settings.session(sid)
        if sc is None:
            raise HTTPException(404, "Sesión inexistente")
        if channel not in sc.channels:
            raise HTTPException(404, f"Canal inexistente. Opciones: {sc.channels}")

    @app.get("/api/sessions/{sid}/stream")
    async def stream(sid: str, request: Request, channel: str = Query("orig")) -> StreamingResponse:
        check(sid, channel)
        key = (sid, channel)
        if not rt.hub.history.get(key):
            rt.hub.seed(key, export.dedupe(await rt.store.read(sid, channel)))
        q = rt.hub.subscribe(key)

        async def gen() -> AsyncIterator[str]:
            try:
                yield "retry: 2000\n\n"
                snap = [c.to_dict() for c in rt.hub.snapshot(key)]
                yield f"event: snapshot\ndata: {json.dumps(snap, ensure_ascii=False)}\n\n"
                while True:
                    try:
                        cap = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {cap.to_json()}\n\n"
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            return
                        yield ": ping\n\n"
            finally:
                rt.hub.unsubscribe(key, q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })

    @app.get("/api/sessions/{sid}/transcript")
    async def transcript(
        sid: str, channel: str = Query("orig"), format: str = Query("txt"), talk: str | None = None,
    ) -> PlainTextResponse:
        check(sid, channel)
        caps = await rt.store.read(sid, channel)
        if talk:
            t = rt.agenda.talk(talk)
            if t is None or t.session != sid:
                raise HTTPException(404, "Charla inexistente en esta sala")
            caps = [c for c in caps if t.start.timestamp() <= c.ts < t.end.timestamp()]
        if format == "srt":
            body, mime, ext = export.to_srt(caps), "application/x-subrip", "srt"
        elif format == "vtt":
            body, mime, ext = export.to_vtt(caps), "text/vtt", "vtt"
        elif format == "txt":
            body, mime, ext = export.to_txt(caps), "text/plain", "txt"
        else:
            raise HTTPException(400, "format debe ser txt, srt o vtt")
        return PlainTextResponse(body, media_type=f"{mime}; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{sid}-{channel}.{ext}"',
        })

    @app.get("/api/sessions/{sid}/summary")
    async def summary(sid: str, channel: str = Query("es"), minutes: int = Query(10, ge=2, le=60)) -> JSONResponse:
        check(sid, channel)
        sc = settings.session(sid)
        assert sc is not None and rt.summarizer is not None
        key = (sid, channel, minutes)
        # Muchas personas tocan el botón a la vez: una sola llamada cada `summary_cache_s`.
        lock = rt.summary_locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = rt.summary_cache.get(key)
            if hit and hit[0] > time.time():
                return JSONResponse(hit[1])
            since = time.time() - minutes * 60
            use_orig = rt.summarizer.mode != "extractivo"
            source_channel = "orig" if use_orig else channel
            caps = [c for c in export.dedupe(await rt.store.read(sid, source_channel)) if c.ts >= since]
            talk = rt.agenda.current(sid)
            target = sc.channel_lang(channel) if channel != "facil" else "facil"
            if channel == "orig" and target == "auto":
                target = caps[-1].lang if caps else "es"
            try:
                bullets, mode = await rt.summarizer.summarize(sid, [c.text for c in caps], minutes, target, talk.context() if talk else "")
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] resumen falló: %s", sid, e)
                raise HTTPException(503, "No se pudo generar el resumen. Probá de nuevo en unos segundos.")
            body = {
                "bullets": bullets, "mode": mode, "minutes": minutes, "segments": len(caps),
                "talk": talk.public() if talk else None, "generated_at": time.time(),
            }
            rt.summary_cache[key] = (time.time() + settings.summary_cache_s, body)
            return JSONResponse(body)

    @app.get("/api/sessions/{sid}/explain")
    async def explain(sid: str, seg: int, channel: str = Query("es")) -> JSONResponse:
        check(sid, channel)
        sc = settings.session(sid)
        assert sc is not None and rt.explainer is not None
        key = (sid, channel, seg)
        lock = rt.explain_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in rt.explain_cache:
                return JSONResponse(rt.explain_cache[key])
            finals = [c for c in rt.hub.snapshot((sid, channel)) if c.final]
            if not any(c.seg == seg for c in finals):
                finals = export.dedupe(await rt.store.read(sid, channel))
            idx = next((i for i, c in enumerate(finals) if c.seg == seg), None)
            if idx is None:
                raise HTTPException(404, "Esa frase ya no está disponible.")
            cap = finals[idx]
            before = [c.text for c in finals[max(0, idx - 2):idx]]
            talk = rt.agenda.current(sid)
            target = "facil" if channel == "facil" else (cap.lang if channel == "orig" else sc.channel_lang(channel))
            try:
                data, mode = await rt.explainer.explain(sid, cap.text, before, target, talk.context() if talk else "")
            except Exception as e:  # noqa: BLE001
                log.warning("[%s] explicación falló: %s", sid, e)
                raise HTTPException(503, "No se pudo generar la explicación. Probá de nuevo en unos segundos.")
            body = {"seg": seg, "text": cap.text, "mode": mode, **data}
            if len(rt.explain_cache) > 5000:
                rt.explain_cache.clear()
            rt.explain_cache[key] = body
            rt.explain_locks.pop(key, None)
            return JSONResponse(body)

    @app.get("/api/sessions/{sid}/pace")
    async def pace(sid: str) -> JSONResponse:
        sc = settings.session(sid)
        if sc is None:
            raise HTTPException(404, "Sesión inexistente")
        st = (await rt.status()).get(sid, {})
        talk = rt.agenda.current(sid)
        caps = [c for c in rt.hub.snapshot((sid, "orig")) if c.final]
        return JSONResponse({
            **(st.get("pace") or {"state": "idle", "wpm": None, "lag_s": None}),
            "lang": sc.source_lang,
            "name": sc.name,
            "last_text": caps[-1].text if caps else "",
            "talk": talk.public() if talk else None,
            "thresholds": {
                "wpm_warn": settings.pace_wpm_warn, "wpm_alert": settings.pace_wpm_alert,
                "lag_warn_s": settings.pace_lag_warn_s, "lag_alert_s": settings.pace_lag_alert_s,
            },
        }, headers={"Cache-Control": "no-cache"})

    @app.websocket("/ingest/{sid}")
    async def ingest(ws: WebSocket, sid: str, token: str = "") -> None:
        w = rt.workers.get(sid)
        expected = settings.caster_token or ""
        await ws.accept()  # aceptamos para poder cerrar con un código que el navegador entienda
        if w is None or w.caster is None:
            await ws.close(code=4404, reason="Esta sesión no recibe audio desde el navegador en este nodo")
            return
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            await ws.close(code=4401, reason="Token inválido (configurá VOZVIVA_CASTER_TOKEN)")
            return
        src = w.caster
        me = object()
        src.attach(me)
        log.info("[%s] caster conectado", sid)
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if data and src.is_owner(me):
                    src.push(data)
                elif not src.is_owner(me):
                    await ws.close(code=4409, reason="Otro caster tomó esta sesión")
                    break
        except WebSocketDisconnect:
            pass
        finally:
            src.detach(me)
            log.info("[%s] caster desconectado", sid)

    return app
