# Wan2.2-S2V Telegram Bot

Bot de Telegram que consume el endpoint RunPod Serverless de Wan2.2-S2V
(imagen + audio -> video con audio-driven animation).

## Flujo de uso (desde Telegram)

1. `/start` — instrucciones.
2. Envía una **foto** (imagen de referencia).
3. Envía un **audio o nota de voz** (el que dirige el movimiento/labios).
4. Envía el **texto del prompt** describiendo la escena, o usa `/generate <prompt>`.
5. El bot manda el job a RunPod, hace polling del estado, y cuando termina
   sube el video resultante como respuesta.
6. `/reset` — borra la imagen/audio guardados para tu usuario y empieza de cero.

Nota: tras generar un video, la sesión del usuario se limpia automáticamente
para no acumular imágenes/audios en memoria.

## Variables de entorno

| Variable                     | Obligatoria | Descripción                                             |
|-------------------------------|:-----------:|----------------------------------------------------------|
| `TELEGRAM_BOT_TOKEN`          | ✅           | Token de @BotFather                                      |
| `RUNPOD_API_KEY`              | ✅           | API key de RunPod                                        |
| `RUNPOD_ENDPOINT_ID`          | ✅           | ID de tu endpoint serverless                              |
| `RUNPOD_USE_LIGHTNING_LORA`   | ❌ (default `true`) | `true` = 4 steps/cfg 1.0 rápido, `false` = 20 steps/cfg 6.0 |
| `RUNPOD_WIDTH` / `RUNPOD_HEIGHT` | ❌ (default `640`/`640`) | Resolución del video                                |
| `RUNPOD_NUM_EXTENDS`          | ❌ (default `1`)    | Debe coincidir con lo soportado por `workflow_api.json` del worker |
| `RUNPOD_POLL_INTERVAL_S`      | ❌ (default `5`)    | Segundos entre cada consulta de estado                    |
| `RUNPOD_JOB_TIMEOUT_S`        | ❌ (default `1800`) | Timeout total esperando el resultado                       |

## Correr localmente

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="123456:ABC..."
export RUNPOD_API_KEY="rpa_..."
export RUNPOD_ENDPOINT_ID="tu-endpoint-id"

python bot.py
```

## Correr con Docker

```bash
docker build -t wan22-s2v-telegram-bot .
docker run -d --name wan22-bot \
  -e TELEGRAM_BOT_TOKEN="123456:ABC..." \
  -e RUNPOD_API_KEY="rpa_..." \
  -e RUNPOD_ENDPOINT_ID="tu-endpoint-id" \
  wan22-s2v-telegram-bot
```

## Notas

- El bot usa **polling** (no requiere webhook público). Para producción a
  gran escala podrías migrar a webhooks, pero para uso personal/pequeño
  grupo, polling es suficiente y más simple de desplegar.
- Los archivos de imagen/audio se mandan en base64 dentro del `input` del job
  (`image_base64` / `audio_base64`), tal como espera `handler.py` del worker
  RunPod que armamos antes.
- Si tu RunPod endpoint hace cold-start (contenedor no está "warm"), la
  primera generación puede tardar bastante más (carga de modelos ~14B en
  VRAM). El bot ya maneja esto con timeout configurable y mensajes de estado
  periódicos cada ~30s.
- Asegúrate que `RUNPOD_NUM_EXTENDS` no supere lo que tu `workflow_api.json`
  soporta actualmente (por defecto ese workflow trae 1 sola extensión
  horneada, como se documentó en el proyecto del worker).
