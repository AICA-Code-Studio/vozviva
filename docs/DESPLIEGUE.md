# Guía de despliegue para una conferencia

Esta guía explica cómo montar Vozviva en un evento con varias salas. Está escrita para que la pueda seguir cualquier equipo organizador, no solo quien escribió el código.

## 1. Qué necesitás

- Un servidor con Python 3.11+ (o Docker) y ffmpeg. Para hasta ~10 salas alcanza una máquina chica (2 vCPU, 2 GB de RAM): el reconocimiento corre en Gemini, el servidor solo mueve audio y texto. Es una estimación; medilo con tu carga.
- Una API key de Gemini con cuota suficiente para N salas en paralelo. Revisá los límites de conexiones simultáneas de la Live API para tu plan antes del evento.
- Por sala, una forma de sacar el audio del escenario (ver punto 3).
- Un dominio con HTTPS (necesario para `/caster` y recomendable para la audiencia).

## 2. Instalación

```bash
git clone <tu-repo> vozviva && cd vozviva
cp config.example.yaml config.yaml
cp .env.example .env    # GEMINI_API_KEY, VOZVIVA_CASTER_TOKEN
docker compose up -d --build vozviva
```

Poné un proxy inverso con HTTPS delante (Caddy, nginx, Traefik). Con nginx, desactivá el buffering para las rutas SSE:

```nginx
location /api/ {
  proxy_pass http://127.0.0.1:8000;
  proxy_buffering off;
  proxy_read_timeout 1h;
}
location /ingest/ {
  proxy_pass http://127.0.0.1:8000;
  proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade;
  proxy_set_header Connection "upgrade";
  proxy_read_timeout 1h;
}
location / { proxy_pass http://127.0.0.1:8000; }
```

## 3. Llevar el audio de cada sala

Siempre que se pueda, tomá el audio **de la consola de sonido** (salida auxiliar o matriz), no de un micrófono en la sala: sin eco ni aplausos, el reconocimiento mejora mucho.

**Opción A: notebook con navegador (la más simple).**
Salida auxiliar de la consola → placa de audio USB → notebook. Abrí `https://<tu-dominio>/caster?s=<sala>`, ingresá la clave, elegí la entrada y tocá "Empezar a enviar". La página desactiva cancelación de eco y control automático de ganancia. Dejala enchufada y con la pestaña abierta. En la configuración, la sala lleva `input: caster`.

**Opción B: OBS o encoder por SRT (la más robusta).**
Si la sala ya transmite con OBS, agregá una salida SRT al servidor:
`srt://<servidor>:9001?mode=caller` y en la configuración `input: srt://0.0.0.0:9001?mode=listener`. Usá un puerto por sala. Verificá que el ffmpeg del servidor tenga soporte SRT (`ffmpeg -protocols | grep srt`).

**Opción C: la transmisión que ya existe.**
Si cada sala ya sale por RTMP/HLS hacia el streaming del evento, podés leer esa misma señal (`input: https://.../sala1.m3u8`). Suma la demora del streaming (a veces 10 s o más), así que usala como respaldo.

**Opción D: placa de audio directa en el servidor** (`input_format: alsa`, `input: hw:1,0`), útil si el servidor está en la cabina.

## 4. Configurar las salas

Cada sala en `config.yaml` tiene:

- `id`: corto, sin espacios (va en la URL).
- `name` y `description`: lo que ve la audiencia.
- `source_lang`: idioma de la charla (`en`, `es`, ...) o `auto` si cambia.
- `input`: de dónde sale el audio.

Completá el `glossary` con el nombre del evento, sponsors, productos y apellidos de speakers: mejora mucho la precisión.

Validá con `python -m vozviva check -c config.yaml`.

## 4 bis. Cargar la agenda

Copiá `agenda.example.yaml` a `agenda.yaml`, cargá cada charla con su sala, horario, título, oradores y resumen del programa, y apuntalo desde `config.yaml` (`agenda: agenda.yaml`). Con eso:

- la audiencia ve qué charla está en curso y quién habla;
- el reconocimiento recibe los nombres de los oradores y los términos técnicos de cada charla, que es donde más se equivoca;
- la traducción y los resúmenes conocen el tema de la charla.

El archivo se relee solo al cambiar: si una charla se atrasa, corregí el horario y guardá. Si el archivo tiene un error, los subtítulos siguen funcionando y `/ops` muestra el problema arriba de la tabla.

## 4 ter. Imprimir los carteles

Abrí `/carteles`, poné la dirección pública que va a usar el público (la del dominio con HTTPS, no la IP interna) y tocá "Imprimir carteles". Sale un A4 por sala con su QR. Probá escanear uno antes de imprimir todos.

## 4 quater. Monitor para el orador

En cada escenario, abrí `/orador?s=<sala>` en la pantalla de retorno (o en una tablet frente al atril) y tocá "Pantalla completa". Mientras el ritmo es bueno la pantalla queda oscura y discreta; se pone amarilla o roja cuando conviene hablar más despacio. Avisale a cada orador antes de subir qué significa: es una ayuda para la accesibilidad, no una evaluación.

## 5. Escalar a muchas salas

Con un solo servidor no hace falta nada más. Si querés repartir la carga:

1. Levantá Redis y poné `VOZVIVA_REDIS_URL` en todos los nodos.
2. Nodos de audio: `python -m vozviva serve --sessions auditorio,sala-2` (cada nodo procesa un subconjunto de salas). Los `/caster` de esas salas tienen que apuntar a ese nodo.
3. Nodos web para la audiencia: `python -m vozviva serve --sessions none`, detrás de un balanceador.

La transcripción y el estado se guardan en Redis, así que cualquier nodo web puede servir exportaciones y el tablero `/ops`.

## 6. Costos

`/ops` muestra los tokens consumidos por sala y, si cargás los precios vigentes en `prices` (USD por millón de tokens, por modelo), el costo estimado. No trae precios por defecto para no mostrar números desactualizados.

El costo depende de las horas de audio por sala y del precio vigente de los modelos. Calculalo con la [página de precios de Gemini](https://ai.google.dev/gemini-api/docs/pricing) para `gemini-3.5-transcribe-live` (por tiempo o tokens de audio) y `gemini-3.5-flash-lite` (texto de traducción, poco volumen). Antes del evento, corré una sala durante una hora con audio real y mirá el consumo en Google AI Studio: es la forma confiable de estimar.

El modo `local` no tiene costo de API pero necesita GPU en el servidor.

## 7. Checklist del día del evento

- [ ] `/ops` muestra todas las salas y el nivel de audio se mueve cuando hablan.
- [ ] Probaste cada sala con una persona hablando al micrófono del escenario.
- [ ] Los carteles de `/carteles` están impresos y pegados en cada sala (y escaneaste al menos uno).
- [ ] La agenda está cargada y `/ops` no muestra errores de agenda.
- [ ] Las notebooks con `/caster` están enchufadas, con suspensión desactivada.
- [ ] Alguien del equipo tiene `/ops` abierto durante todo el evento.
- [ ] En `/ops`, la latencia "voz → texto" está en valores aceptables para tu evento después de la primera charla.

## 8. Problemas frecuentes

| Síntoma | Causa probable | Qué hacer |
|---|---|---|
| "Último audio" en rojo | La fuente se cortó | Revisar cable/placa, OBS o la pestaña de `/caster` |
| Hay audio pero no hay subtítulos | Error de API o cuota | Ver la columna de errores en `/ops` y los logs |
| Subtítulos con palabras equivocadas en nombres | Falta en el glosario | Agregar el término y reiniciar |
| La audiencia no ve nada detrás del proxy | Buffering de SSE | `proxy_buffering off` en `/api/` |
| `/caster` no pide el micrófono | Sin HTTPS | Servir con HTTPS o usar `localhost` |
