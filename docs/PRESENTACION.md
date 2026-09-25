# Presentación para la Vibeathon

Completá lo que está entre corchetes antes de enviar. No afirmes resultados que no hayas medido: si no llegaste a probar algo con la API real, decilo.

## Texto corto (para el formulario)

**Vozviva: subtítulos en vivo, abiertos, para todas las salas de una conferencia.**

Vozviva toma el audio de cada escenario y publica subtítulos en tiempo real en el idioma original, en español y en portugués (y en inglés cuando la charla es en español). La audiencia escanea el QR del cartel de la sala, elige idioma y lee desde el celular, o escucha el texto en voz alta. Además del original y la traducción, ofrece un canal en español de Lectura Fácil, un botón "¿Qué me perdí?" que resume los últimos minutos para quien llega tarde, y cualquiera puede tocar una frase que no entendió para recibir su explicación. Del lado del escenario, un monitor le avisa al orador cuando habla demasiado rápido para que los subtítulos lo sigan: la accesibilidad también depende de quien habla. Usa `gemini-3.5-transcribe-live` para transcribir en streaming y Gemini Flash-Lite para traducir cada frase con contexto y un glosario del evento; también tiene un modo 100 % local con Whisper y Gemma.

Está pensado para operarse en un evento real: cada sala es independiente, el audio entra desde la consola por SRT/RTMP o desde una notebook con el navegador, carga la agenda del evento para mostrar la charla en curso y armar solo el glosario de cada sala (nombres de oradores, siglas, productos), hay un tablero que muestra qué sala se quedó sin audio, con latencia medida, tokens y costo, lectura en voz alta para personas ciegas, overlay para OBS y exportación a SRT/VTT para publicar junto a los videos. Escala repartiendo salas entre máquinas con Redis. Licencia MIT, con guía de despliegue para que cualquier conferencia lo use.

Repositorio: [URL]
Demo: [URL del video]
Autor: Matias Ezequiel Guido (proyecto individual, co-creado con Claude, de Anthropic)

## Guion de demo (3 a 3 minutos y medio)

1. **El problema (15 s).** Nerdearla tiene más de 30 sesiones en inglés; hoy se resuelve con herramientas comerciales caras y operación manual.
2. **La audiencia (40 s).** Abrir la web en el celular, elegir una sala, mostrar el texto apareciendo mientras suena una charla real (archivo con `realtime: true`). Cambiar entre Original y Español. Agrandar la letra. Tocar "Escuchar" y mostrar que la traducción se lee en voz alta (accesibilidad para personas ciegas).
3. **Accesibilidad (30 s).** Cambiar a "Fácil" y mostrar la misma frase en Lectura Fácil. Tocar "¿Qué me perdí?" y mostrar el resumen de los últimos minutos. Tocar una frase con un término técnico y mostrar la explicación. Señalar el título y el orador de la charla en curso, que salen de la agenda.
4 bis. **El escenario (20 s).** Mostrar `/orador` en una pantalla: hablar rápido a propósito frente al micrófono y ver cómo pasa a amarillo o rojo.
4. **Varias salas (30 s).** Mostrar `/ops` con varias salas a la vez: niveles de audio, latencia medida y consumo. Mostrar `/carteles` con el cartel impreso de una sala y escanearlo con el celular en cámara.
5. **Operación (20 s).** Abrir `/caster` en otra pestaña y mandar audio desde el micrófono a una sala. Mostrar el overlay en OBS.
6. **Abierto y desplegable (20 s).** Licencia MIT, `docker compose up`, guía de despliegue, modo local con Gemma.
7. **Cierre (10 s).** Qué falta y qué sigue: audio traducido, panel de glosario en vivo.

## Antes de grabar

- Correr `python scripts/probar_gemini.py <audio>` y confirmar que la transcripción llega bien.
- Usar un audio con licencia que permita mostrarlo (por ejemplo, una charla propia o con licencia Creative Commons).
- Grabar con el celular en la mano o con la vista móvil del navegador: la experiencia de la audiencia es el punto fuerte.

## Sugerencia para los commits

Si subís el código con git, podés dejar constancia de la co-creación con la convención de GitHub:

```
git commit -m "Primera versión de Vozviva" -m "Co-authored-by: Claude <noreply@anthropic.com>"
```
