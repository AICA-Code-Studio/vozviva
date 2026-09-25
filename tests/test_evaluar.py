import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("evaluar", Path(__file__).parent.parent / "scripts" / "evaluar.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_wer():
    ref = ev.normalize("Hoy usamos Kubernetes y OpenTelemetry.")
    assert ev.wer(ref, ref)[0] == 0.0
    w, S, D, I = ev.wer(ref, ev.normalize("hoy usamos cubernetes y open telemetry"))
    assert (S, D, I) == (2, 0, 1) and abs(w - 3 / 5) < 1e-9
    assert ev.normalize("k8s.io, gpt-4.") == ["k8s.io", "gpt-4"]


def test_terms():
    assert ev.count_term("SLO", "Definan un SLO. Los SLOs y el SLO.") == 2
