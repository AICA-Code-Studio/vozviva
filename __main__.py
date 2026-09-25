"""Línea de comandos: `python -m vozviva serve -c config.yaml`."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vozviva", description="Subtítulos en vivo para conferencias")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Levanta la web y los workers de las sesiones")
    s.add_argument("-c", "--config", default=os.environ.get("VOZVIVA_CONFIG", "config.yaml"))
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    s.add_argument(
        "--sessions", default=os.environ.get("VOZVIVA_SESSIONS", "all"),
        help="Qué sesiones procesa este nodo: all | none | id1,id2 (para repartir salas entre máquinas)",
    )
    s.add_argument("--backend", help="Pisa el backend de la configuración (p. ej. mock)")
    s.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    c = sub.add_parser("check", help="Valida la configuración y muestra las sesiones")
    c.add_argument("-c", "--config", default=os.environ.get("VOZVIVA_CONFIG", "config.yaml"))
    args = p.parse_args(argv)

    logging.basicConfig(level=getattr(args, "log_level", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)

    if args.cmd == "check":
        print(f"backend: {settings.backend} | traductor: {settings.translator} | redis: {bool(settings.redis_url)}")
        for sc in settings.sessions:
            print(f"- {sc.id}: {sc.name} | idioma {sc.source_lang} | entrada {sc.input} | canales {', '.join(sc.channels)}")
        if settings.backend.startswith("gemini") and not settings.gemini_api_key:
            print("ATENCIÓN: falta GEMINI_API_KEY", file=sys.stderr)
            return 1
        return 0

    if args.backend:
        settings.backend = args.backend
    if args.sessions == "all":
        run = None
    elif args.sessions == "none":
        run = []
    else:
        run = [x.strip() for x in args.sessions.split(",") if x.strip()]

    import uvicorn

    from .server import create_app

    app = create_app(settings, run)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower(), proxy_headers=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
