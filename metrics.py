"""Métricas por sala: latencia y consumo de la API.

Los tokens salen de `usage_metadata` de cada respuesta de Gemini. Para la Live
API se suman los valores de cada mensaje; conviene contrastar el total con la
consola de Google AI Studio antes de confiar en el costo estimado.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


def usage_counts(u: Any) -> tuple[int, int]:
    if u is None:
        return 0, 0
    pin = getattr(u, "prompt_token_count", None) or 0
    pout = getattr(u, "candidates_token_count", None) or getattr(u, "response_token_count", None) or 0
    return int(pin), int(pout)


class Meter:
    def __init__(self) -> None:
        self.tokens: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"input": 0, "output": 0, "calls": 0})

    def add(self, session: str, model: str, usage: Any) -> None:
        pin, pout = usage_counts(usage)
        if not (pin or pout):
            return
        rec = self.tokens[(session, model)]
        rec["input"] += pin
        rec["output"] += pout
        rec["calls"] += 1

    def report(self, session: str, prices: dict[str, dict[str, float]]) -> dict[str, Any]:
        by_model = {m: dict(v) for (s, m), v in self.tokens.items() if s == session}
        cost: float | None = 0.0 if prices else None  # sin precios configurados no estimamos
        for m, v in by_model.items():
            p = prices.get(m)
            if p is None:
                cost = None if v["input"] or v["output"] else cost
                continue
            c = v["input"] * float(p.get("input", 0)) / 1e6 + v["output"] * float(p.get("output", 0)) / 1e6
            v["cost_usd"] = round(c, 4)
            if cost is not None:
                cost += c
        return {
            "tokens_input": sum(v["input"] for v in by_model.values()),
            "tokens_output": sum(v["output"] for v in by_model.values()),
            "cost_usd": round(cost, 4) if cost is not None else None,
            "by_model": by_model,
        }


class Latency:
    """Percentiles sobre las últimas N mediciones, en segundos."""

    def __init__(self, n: int = 60) -> None:
        self.values: deque[float] = deque(maxlen=n)

    def add(self, v: float) -> None:
        if 0 <= v < 120:
            self.values.append(v)

    def summary(self) -> dict[str, float | int | None]:
        if not self.values:
            return {"p50": None, "p95": None, "n": 0}
        xs = sorted(self.values)
        pick = lambda q: round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 2)  # noqa: E731
        return {"p50": pick(0.5), "p95": pick(0.95), "n": len(xs)}


METER = Meter()
