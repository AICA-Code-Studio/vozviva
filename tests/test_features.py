"""Agenda, lectura fácil, resúmenes, QR y métricas."""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml
from fastapi.testclient import TestClient

from vozviva.agenda import Agenda, extract_terms
from vozviva.config import compute_channels, load_settings
from vozviva.metrics import Latency, Meter
from vozviva.models import Caption
from vozviva.server import create_app
from vozviva.summarize import extractive

TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def write_agenda(path, now):
    fmt = lambda d: d.strftime("%Y-%m-%d %H:%M")  # noqa: E731
    path.write_text(yaml.safe_dump({"talks": [
        {"id": "obs", "session": "auditorio", "start": fmt(now - timedelta(minutes=20)), "end": fmt(now + timedelta(minutes=25)),
         "title": "Observabilidad con OpenTelemetry", "speakers": ["Ana Pérez"], "abstract": "Usamos Grafana y Prometheus en K8s.",
         "glossary": ["SLO"]},
        {"id": "k8s", "session": "auditorio", "start": fmt(now + timedelta(minutes=30)), "end": fmt(now + timedelta(minutes=70)),
         "title": "Migrar a Kubernetes", "speakers": ["Juan Gómez"]},
    ]}, allow_unicode=True), encoding="utf-8")


def test_channels_easy_read():
    assert compute_channels("en", ["es"], "en", True) == ["orig", "es", "facil"]
    assert compute_channels("es", ["es"], "en", True) == ["orig", "en", "facil"]


def test_agenda(tmp_path):
    now = datetime.now(TZ)
    f = tmp_path / "agenda.yaml"
    write_agenda(f, now)
    a = Agenda(str(f), "America/Argentina/Buenos_Aires", ["auditorio"])
    assert a.error is None
    cur = a.current("auditorio")
    assert cur and cur.id == "obs"
    assert a.next("auditorio").id == "k8s"
    terms = cur.terms()
    assert terms[:2] == ["Ana Pérez", "SLO"] and "OpenTelemetry" in terms and "Grafana" in terms and "K8s" in terms
    assert a.relevant("auditorio", now + timedelta(minutes=26)).id == "k8s"  # faltan 4 min: ya prepara el glosario
    assert a.current("otra") is None
    assert "Ana Pérez" in cur.context()


def test_agenda_bad_file_does_not_crash(tmp_path):
    f = tmp_path / "agenda.yaml"
    f.write_text("talks: [{session: x}]")
    a = Agenda(str(f), "UTC", ["x"])
    assert a.error and a.current("x") is None


def test_extract_terms_skips_sentence_start():
    assert extract_terms("Hoy hablamos de Rust y WebAssembly.") == ["Rust", "WebAssembly"]


def test_latency_and_cost():
    lat = Latency()
    for v in [0.5, 0.7, 0.9, 1.1, 3.0]:
        lat.add(v)
    s = lat.summary()
    assert s["p50"] == 0.9 and s["p95"] == 3.0 and s["n"] == 5

    class U:
        prompt_token_count = 1_000_000
        response_token_count = 500_000

    m = Meter()
    m.add("s", "modelo-a", U())
    assert m.report("s", {})["cost_usd"] is None  # sin precios no inventamos costo
    r = m.report("s", {"modelo-a": {"input": 1.0, "output": 2.0}})
    assert r["cost_usd"] == 2.0 and r["tokens_input"] == 1_000_000


def test_extractive_summary():
    lines = [f"frase {i}" for i in range(10)]
    assert extractive(lines) == ["frase 0", "frase 3", "frase 6", "frase 9"]
    assert extractive(["a"]) == ["a"]


def test_server_features(tmp_path, monkeypatch):
    now = datetime.now(TZ)
    write_agenda(tmp_path / "agenda.yaml", now)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "backend": "mock", "translator": "none", "caster_token": "x", "data_dir": str(tmp_path / "data"),
        "agenda": str(tmp_path / "agenda.yaml"), "public_url": "https://subs.example.org",
        "sessions": [{"id": "auditorio", "name": "Auditorio", "source_lang": "en", "input": "caster"}],
    }))
    settings = load_settings(cfg)
    with TestClient(create_app(settings, run_sessions=[])) as c:
        rt = c.app.state.runtime
        s = c.get("/api/sessions").json()[0]
        assert [ch["id"] for ch in s["channels"]] == ["orig", "es", "pt", "facil"]
        assert s["channels"][2]["label"] == "Português" and s["channels"][3]["label"] == "Español fácil"
        assert s["now"]["title"] == "Observabilidad con OpenTelemetry" and s["next"]["id"] == "k8s"

        r = c.get("/api/sessions/auditorio/summary?channel=facil&minutes=10").json()
        assert r["bullets"] == [] and r["mode"] == "extractivo"

        import asyncio
        for i in range(6):
            asyncio.run(rt.store.append(Caption("auditorio", "facil", i, f"Frase fácil {i}.", True, i, i + 1, "es", ts=time.time())))
        rt.summary_cache.clear()
        r = c.get("/api/sessions/auditorio/summary?channel=facil&minutes=10").json()
        assert len(r["bullets"]) == 4 and r["talk"]["id"] == "obs"

        q = c.get("/api/sessions/auditorio/qr.svg")
        assert q.status_code == 200 and q.text.lstrip().startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in q.text
        assert q.headers["x-vozviva-url"] == "https://subs.example.org/?s=auditorio"
        assert c.get("/carteles").status_code == 200
        assert c.get("/api/agenda").json()["talks"][0]["session"] == "auditorio"

        tx = c.get("/api/sessions/auditorio/transcript?channel=facil&talk=obs").text
        assert "Frase fácil 0." in tx
        assert c.get("/api/sessions/auditorio/transcript?channel=facil&talk=nada").status_code == 404


def test_explain_and_pace_endpoints(tmp_path):
    from vozviva.explain import demo_explain

    assert [t["term"] for t in demo_explain("Uso OpenTelemetry y Grafana para la observabilidad.")["terms"]] == [
        "observabilidad", "OpenTelemetry", "Grafana"]

    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "backend": "mock", "translator": "none", "caster_token": "x", "data_dir": str(tmp_path / "data"),
        "sessions": [{"id": "sala", "name": "Sala", "source_lang": "en", "input": "caster"}],
    }))
    settings = load_settings(cfg)
    with TestClient(create_app(settings, run_sessions=[])) as c:
        rt = c.app.state.runtime
        rt.hub.dispatch(Caption("sala", "es", 7, "Migramos el monolito a Kubernetes.", True, 0, 2, "es"))
        r = c.get("/api/sessions/sala/explain?channel=es&seg=7").json()
        assert r["mode"] == "demo" and {t["term"] for t in r["terms"]} == {"monolito", "Kubernetes"}
        assert ("sala", "es", 7) in rt.explain_cache
        assert c.get("/api/sessions/sala/explain?channel=es&seg=99").status_code == 404
        p = c.get("/api/sessions/sala/pace").json()
        assert p["state"] == "idle" and p["lang"] == "en"
        assert c.get("/orador").status_code == 200


def test_worker_pace_states(tmp_path):
    from vozviva.bus import FileStore, Hub, Publisher
    from vozviva.config import SessionConfig, Settings
    from vozviva.worker import SessionWorker

    settings = Settings(backend="mock", data_dir=str(tmp_path))
    sc = SessionConfig(id="s", name="s", source_lang="es", input="caster")
    sc.channels = ["orig"]
    w = SessionWorker(sc, settings, Publisher(Hub(), FileStore(str(tmp_path))), None)
    assert w.pace()["state"] == "idle"
    now = time.time()
    w.last_caption = now
    for i in range(10):  # 10 frases de 15 palabras en 30 s -> 300 ppm
        w._words.append((now - 30 + i * 3, 15))
    p = w.pace()
    assert p["state"] == "alert" and 250 <= p["wpm"] <= 310
    w._words.clear()
    for i in range(10):  # 10 frases de 8 palabras en 30 s -> ~160 ppm
        w._words.append((now - 30 + i * 3, 8))
    assert w.pace()["state"] == "ok"
    for v in [4.5, 4.8, 5.0]:
        w.lat_asr.add(v)
    assert w.pace()["state"] == "warn"  # ritmo bien, pero los subtítulos vienen atrasados
