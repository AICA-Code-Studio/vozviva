"""Backend de demostración: no necesita API key ni audio real.

Genera frases de ejemplo palabra por palabra (parciales) y las cierra con sus
traducciones. Sirve para probar la interfaz, el despliegue y la carga de
audiencia sin gastar créditos.
"""
from __future__ import annotations

import asyncio
import random
from typing import AsyncIterator

from ..models import TranscriptEvent
from .base import Backend, Emit

EN = [
    ("Welcome everyone, thanks for joining this session.", "Bienvenidos a todos, gracias por sumarse a esta sesión."),
    ("Today we are going to talk about observability in distributed systems.", "Hoy vamos a hablar de observabilidad en sistemas distribuidos."),
    ("The first thing you need is a clear definition of what healthy means.", "Lo primero que necesitás es una definición clara de qué significa saludable."),
    ("Most outages are not caused by a single failure but by a chain of small ones.", "La mayoría de las caídas no las causa una sola falla, sino una cadena de fallas pequeñas."),
    ("Let me show you a quick demo with OpenTelemetry and Grafana.", "Les muestro una demo rápida con OpenTelemetry y Grafana."),
    ("If you remember one thing from this talk, measure before you optimize.", "Si recuerdan una sola cosa de esta charla, midan antes de optimizar."),
]
ES = [
    ("Buenas tardes, gracias por venir a esta charla.", "Good afternoon, thanks for coming to this talk."),
    ("Vamos a ver cómo migramos un monolito a Kubernetes sin cortar el servicio.", "We will see how we migrated a monolith to Kubernetes without downtime."),
    ("El error más caro que cometimos fue no automatizar las pruebas de carga.", "The most expensive mistake we made was not automating load tests."),
    ("Todo el código está publicado con licencia libre en GitHub.", "All the code is published under a free license on GitHub."),
]

# Traducciones al portugués (escritas a mano para la demo), indexadas por la frase original.
PT = {
    "Welcome everyone, thanks for joining this session.": "Bem-vindos a todos, obrigado por participarem desta sessão.",
    "Today we are going to talk about observability in distributed systems.": "Hoje vamos falar sobre observabilidade em sistemas distribuídos.",
    "The first thing you need is a clear definition of what healthy means.": "A primeira coisa de que você precisa é uma definição clara do que significa saudável.",
    "Most outages are not caused by a single failure but by a chain of small ones.": "A maioria das quedas não é causada por uma única falha, mas por uma cadeia de pequenas falhas.",
    "Let me show you a quick demo with OpenTelemetry and Grafana.": "Vou mostrar uma demonstração rápida com OpenTelemetry e Grafana.",
    "If you remember one thing from this talk, measure before you optimize.": "Se vocês lembrarem de uma coisa desta palestra, meçam antes de otimizar.",
    "Buenas tardes, gracias por venir a esta charla.": "Boa tarde, obrigado por virem a esta palestra.",
    "Vamos a ver cómo migramos un monolito a Kubernetes sin cortar el servicio.": "Vamos ver como migramos um monólito para o Kubernetes sem interromper o serviço.",
    "El error más caro que cometimos fue no automatizar las pruebas de carga.": "O erro mais caro que cometemos foi não automatizar os testes de carga.",
    "Todo el código está publicado con licencia libre en GitHub.": "Todo o código está publicado com licença livre no GitHub.",
}

# Versiones en Lectura Fácil (escritas a mano para la demo).
EASY = {
    "Bienvenidos a todos, gracias por sumarse a esta sesión.": "Hola a todos. Gracias por venir a esta charla.",
    "Hoy vamos a hablar de observabilidad en sistemas distribuidos.": "Hoy hablamos de observabilidad. Observabilidad es poder ver qué pasa adentro de un sistema.",
    "Lo primero que necesitás es una definición clara de qué significa saludable.": "Primero, definí qué significa que el sistema funciona bien.",
    "La mayoría de las caídas no las causa una sola falla, sino una cadena de fallas pequeñas.": "Casi siempre un sistema se cae por varios errores chicos. No por un solo error grande.",
    "Les muestro una demo rápida con OpenTelemetry y Grafana.": "Ahora muestro un ejemplo. Uso OpenTelemetry y Grafana. Son herramientas para medir sistemas.",
    "Si recuerdan una sola cosa de esta charla, midan antes de optimizar.": "Lo más importante: primero medí. Después mejorá.",
    "Buenas tardes, gracias por venir a esta charla.": "Buenas tardes. Gracias por venir.",
    "Vamos a ver cómo migramos un monolito a Kubernetes sin cortar el servicio.": "Vamos a contar cómo cambiamos nuestro sistema. Lo pasamos a Kubernetes sin cortar el servicio.",
    "El error más caro que cometimos fue no automatizar las pruebas de carga.": "Nuestro peor error: no probar a tiempo cuánta gente aguanta el sistema.",
    "Todo el código está publicado con licencia libre en GitHub.": "Todo el código es libre. Está en GitHub.",
}


class MockBackend(Backend):
    translates = True
    measures_latency = False

    async def run(self, audio: AsyncIterator[bytes], emit: Emit) -> None:
        drain = asyncio.create_task(self._drain(audio))
        src = self.session.source_lang
        phrases = ES if src == "es" else EN
        t = 0.0
        try:
            while True:
                orig, trans = random.choice(phrases)
                seg = self.next_seg()
                words = orig.split()
                t0 = t
                for i in range(1, len(words) + 1):
                    await asyncio.sleep(random.uniform(0.18, 0.32))
                    t += 0.25
                    await emit(TranscriptEvent(seg=seg, text=" ".join(words[:i]), final=False, t0=t0, t1=t, lang=src))
                spanish = orig if src == "es" else trans
                translations = {}
                for c in self.session.channels:
                    if c == "orig":
                        continue
                    if c == "facil":
                        translations[c] = EASY.get(spanish, spanish)
                    elif c == "en":
                        translations[c] = orig if src != "es" else trans
                    elif c == "pt":
                        translations[c] = PT.get(orig, spanish)
                    else:
                        translations[c] = spanish
                await emit(TranscriptEvent(seg=seg, text=orig, final=True, t0=t0, t1=t, lang=src, translations=translations))
                await asyncio.sleep(random.uniform(0.6, 1.4))
                t += 1.0
        finally:
            drain.cancel()

    @staticmethod
    async def _drain(audio: AsyncIterator[bytes]) -> None:
        async for _ in audio:
            pass
