"""Mide la calidad de una transcripción contra una referencia humana.

Uso:
  python scripts/evaluar.py referencia.txt transcripcion.txt --terminos "Kubernetes,OpenTelemetry,SLO"

- referencia.txt: transcripción correcta del mismo tramo (por ejemplo, subtítulos
  revisados por una persona). Conviene usar fragmentos de 10 a 15 minutos.
- transcripcion.txt: lo que exportó Vozviva (/api/sessions/<sala>/transcript?channel=orig&format=txt).

Informa WER (tasa de error por palabra: menor es mejor) y, para cada término
técnico, cuántas veces aparece en la referencia y cuántas lo reconoció Vozviva.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata


def normalize(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text.lower())
    text = re.sub(r"[^\w\s'+#.-]", " ", text)
    text = re.sub(r"(?<!\w)[.\-']|[.\-'](?!\w)", " ", text)  # puntuación suelta, no la de "k8s.io" o "gpt-4"
    return text.split()


def wer(ref: list[str], hyp: list[str]) -> tuple[float, int, int, int]:
    """Distancia de edición por palabras. Devuelve (wer, sustituciones, borrados, inserciones)."""
    n, m = len(ref), len(hyp)
    if n == 0:
        return (0.0 if m == 0 else 1.0), 0, 0, m
    prev = [(j, 0, 0, j) for j in range(m + 1)]  # (costo, S, D, I)
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [(0, 0, 0, 0)] * m
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                cur[j] = prev[j - 1]
                continue
            s, d, ins = prev[j - 1], prev[j], cur[j - 1]
            best = min(
                (s[0] + 1, s[1] + 1, s[2], s[3]),
                (d[0] + 1, d[1], d[2] + 1, d[3]),
                (ins[0] + 1, ins[1], ins[2], ins[3] + 1),
            )
            cur[j] = best
        prev = cur
    cost, S, D, I = prev[m]
    return cost / n, S, D, I


def count_term(term: str, text: str) -> int:
    return len(re.findall(rf"(?<!\w){re.escape(term.lower())}(?!\w)", text.lower()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("referencia")
    ap.add_argument("transcripcion")
    ap.add_argument("--terminos", default="", help="Lista separada por comas")
    a = ap.parse_args()
    ref_text = open(a.referencia, encoding="utf-8").read()
    hyp_text = open(a.transcripcion, encoding="utf-8").read()
    ref, hyp = normalize(ref_text), normalize(hyp_text)
    if len(ref) * max(len(hyp), 1) > 60_000_000:
        print("El texto es muy largo para este cálculo: usá fragmentos de 10 a 15 minutos.", file=sys.stderr)
        return 1
    w, S, D, I = wer(ref, hyp)
    print(f"Palabras en la referencia: {len(ref)}")
    print(f"WER: {w:.1%}  (sustituciones {S}, palabras omitidas {D}, palabras agregadas {I})")
    terms = [t.strip() for t in a.terminos.split(",") if t.strip()]
    if terms:
        print("\nTérminos técnicos (reconocidos / presentes en la referencia):")
        total_ref = total_ok = 0
        for t in terms:
            r, h = count_term(t, ref_text), count_term(t, hyp_text)
            ok = min(r, h)
            total_ref += r
            total_ok += ok
            print(f"  {t}: {ok}/{r}" + ("" if r else "  (no aparece en la referencia)"))
        if total_ref:
            print(f"Aciertos en términos: {total_ok / total_ref:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
