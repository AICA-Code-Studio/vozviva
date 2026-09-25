# Criterios de evaluación

Cómo responde Vozviva a cada criterio de la Vibeathon, cómo se puede verificar y qué límites conocemos. Preferimos mostrar cómo medir antes que prometer números: los valores reales dependen del audio de cada sala.

## Calidad

*¿La transcripción es precisa y se entiende, incluso con términos técnicos?*

Qué hace:
- Usa `gemini-3.5-transcribe-live`, un modelo dedicado a transcripción en streaming, con vocabulario personalizado y detección de idioma.
- Cada sala arma su glosario desde la agenda: nombres de oradores, siglas, productos y términos del título y el resumen de cada charla, más un glosario global y uno por charla. El glosario se renueva solo cuando cambia la charla.
- La traducción recibe el contexto de la charla, las frases anteriores y el glosario, y mantiene sin traducir productos, comandos y código.
- Modo `SMART` para limpiar muletillas, o `VERBATIM` si se prefiere literal.
- Para que se entienda: letra grande y ajustable, tipografía de alta legibilidad, canal de Lectura Fácil y explicación de cualquier frase con un toque.

Cómo verificarlo:
- `python scripts/evaluar.py referencia.txt transcripcion.txt --terminos "Kubernetes,SLO,..."` calcula la tasa de error por palabra (WER) y cuántos términos técnicos se reconocieron, contra una transcripción humana del mismo tramo.

Límites:
- No medimos todavía el WER con audio real de Nerdearla.
- El glosario automático es heurístico; el campo `glossary` de cada charla lo complementa.

## Latencia

*¿Los subtítulos aparecen con un retraso aceptable para seguir una charla en vivo?*

Qué hace:
- Streaming por WebSocket con la Live API: el texto parcial aparece mientras el orador habla y se reemplaza por el final cuando hace una pausa.
- La traducción se hace por frase terminada, con un modelo rápido (`gemini-3.5-flash-lite`), y una sola llamada devuelve todos los idiomas: sumar portugués no suma llamadas, solo alarga un poco la respuesta.
- La audiencia recibe por Server-Sent Events, sin polling.
- Las conexiones Live se rotan en silencios antes del límite de duración, sin perder audio.
- **Estabilizador de oraciones.** En la prueba con audio real, el modelo mandó parciales cada ~0,5 s pero un solo FINAL por minuto, porque el orador casi no hacía pausas. Vozviva cierra cada oración apenas se estabiliza en los parciales (no cambió entre dos parciales seguidos y el orador siguió hablando), la publica y la traduce enseguida. Cuando llega el FINAL del modelo, más preciso, corrige lo ya publicado en el mismo lugar de la pantalla (en esa prueba, "AGI 3" pasó a "ARC AGI 3") y retraduce solo si cambiaron las palabras.

Cómo verificarlo:
- `/ops` mide por sala la demora hasta el subtítulo final y desde el final hasta la traducción (mediana y p95). Para las oraciones que cierra el estabilizador, se mide desde que la última palabra apareció en pantalla, así que no incluye la demora propia del modelo en mostrar esa palabra (en la prueba real, del orden de medio segundo a un segundo).
- `scripts/probar_gemini.py` muestra con marca de tiempo cada parcial y final que devuelve el modelo.

Límites:
- La latencia depende del modelo y de la red. Probamos la transcripción en vivo con audio real; falta medir la cadena completa con traducción durante una charla entera.

## Escalabilidad

*¿La solución puede correr muchas sesiones en simultáneo sin grandes cambios ni costos prohibitivos?*

Qué hace:
- Cada sala es un worker independiente: agregar una sala es agregar cuatro líneas de configuración.
- El trabajo pesado corre en Gemini; el servidor solo mueve audio y texto, así que una máquina chica atiende varias salas.
- Con Redis, las salas se reparten entre varias máquinas (`--sessions`) y la audiencia se atiende desde nodos web separados, detrás de un balanceador.
- El costo crece con las horas de audio, no con la audiencia: los subtítulos a la audiencia no llaman a la API; los resúmenes se reutilizan 30 segundos y cada explicación de frase se genera una sola vez.
- Traducir a un idioma más no agrega llamadas (van todos en la misma respuesta).
- Alternativa sin costo por uso: backend `local` con Whisper y Gemma en una GPU propia.

Cómo verificarlo:
- `/ops` muestra tokens por sala, tokens por hora de audio y, con los precios cargados en `prices`, el costo por sala y hora. Con eso se proyecta el costo del evento: salas × horas × costo por sala-hora.
- El modo demo levanta 6 salas en paralelo.

Límites:
- No probamos todavía con decenas de salas contra la API real. Hay que revisar el límite de conexiones Live simultáneas del plan de Gemini que se use.
- El backend `gemini_live_translate` abre una conexión por idioma, así que su costo sí crece con los idiomas.

## Despliegue y operación

*¿Qué tan sencilla es la puesta en producción y su operación durante un evento?*

Qué hace:
- `docker compose up` con un archivo de configuración y una clave. Demo sin clave en un comando: `docker compose --profile demo up --build demo`.
- `python -m vozviva check` valida la configuración antes de arrancar.
- Entrada de audio flexible: SRT, RTMP, HLS, placa de audio, archivo, o una notebook con el navegador (`/caster`).
- Tolerancia a fallos: ffmpeg y las conexiones a la API se reconectan solas; si una sala falla, las demás siguen; una agenda con errores no corta los subtítulos.
- `/ops` muestra en una pantalla qué sala se quedó sin audio o sin subtítulos, sus errores y su consumo.
- `/carteles` imprime un cartel A4 con QR por sala; `/orador` es la pantalla de retorno para el escenario.
- La agenda se puede corregir durante el evento: se relee sola.
- Guía paso a paso con checklist del día y tabla de problemas frecuentes: [DESPLIEGUE.md](DESPLIEGUE.md).

Límites:
- El HTTPS (necesario para `/caster`) lo resuelve un proxy delante, no Vozviva.

## Innovación

*¿Se propusieron funcionalidades creativas que aportan valor extra?*

- **Tocá una frase y te la explico:** cualquier persona toca una línea que no entendió y recibe, solo para ella, qué quiso decir el orador y qué significan sus términos.
- **Monitor de ritmo para el orador:** avisa en el escenario cuando se habla demasiado rápido para que los subtítulos y la traducción lo sigan. La accesibilidad también depende de quien habla.
- **Español fácil:** un canal en formato de Lectura Fácil, para personas con discapacidad intelectual, con el español como segunda lengua (entre ellas muchas personas sordas) o que no conocen la jerga.
- **¿Qué me perdí?:** resumen de los últimos minutos para quien llega tarde o cambia de sala.
- **Escuchar:** lectura en voz alta en el celular, para personas ciegas o con baja visión.
- **Agenda integrada:** charla en curso, glosario automático por charla y transcripciones exportables por charla.
- **Carteles con QR** para que el público se entere de que el servicio existe.
- Overlay para OBS y exportación a SRT/VTT para publicar junto a los videos.
