"""
Telegram bot that drives a RunPod Serverless endpoint running Wan2.2-S2V
(image + audio -> talking/singing video), as built in the wan22-s2v-runpod
Dockerfile/handler.

Flow per user:
1. /start                -> instructions
2. send a photo          -> stored as reference image
3. send a voice/audio    -> stored as driving audio
4. send text (prompt)    -> triggers generation, or use /generate <prompt>
5. bot polls RunPod, then sends back the resulting video

Env vars required:
  TELEGRAM_BOT_TOKEN   - token from @BotFather
  RUNPOD_API_KEY       - RunPod API key
  RUNPOD_ENDPOINT_ID   - your serverless endpoint id

Optional env vars:
  RUNPOD_USE_LIGHTNING_LORA (default "true")
  RUNPOD_WIDTH / RUNPOD_HEIGHT (default 640/640)
  RUNPOD_NUM_EXTENDS (default 1)
  RUNPOD_POLL_INTERVAL_S (default 5)
  RUNPOD_JOB_TIMEOUT_S (default 1800)
"""

import asyncio
import base64
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv
import httpx
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("wan22-s2v-bot")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
RUNPOD_API_KEY = os.environ["RUNPOD_API_KEY"]
RUNPOD_ENDPOINT_ID = os.environ["RUNPOD_ENDPOINT_ID"]

RUNPOD_BASE_URL = f"https://api.runpod.ai/v2/{RUNPOD_ENDPOINT_ID}"
RUNPOD_HEADERS = {
    "Authorization": f"Bearer {RUNPOD_API_KEY}",
    "Content-Type": "application/json",
}

USE_LIGHTNING_LORA = os.environ.get("RUNPOD_USE_LIGHTNING_LORA", "true").lower() == "true"
WIDTH = int(os.environ.get("RUNPOD_WIDTH", "640"))
HEIGHT = int(os.environ.get("RUNPOD_HEIGHT", "640"))
NUM_EXTENDS = int(os.environ.get("RUNPOD_NUM_EXTENDS", "1"))
POLL_INTERVAL_S = float(os.environ.get("RUNPOD_POLL_INTERVAL_S", "5"))
JOB_TIMEOUT_S = float(os.environ.get("RUNPOD_JOB_TIMEOUT_S", "1800"))

# workflow_api.json is expected to live next to this script. It's sent to the
# RunPod handler as base64 with every job, so the handler doesn't rely on
# whatever copy is baked into the Docker image.
WORKFLOW_PATH = Path(__file__).resolve().parent / "workflow_api.json"


def _load_workflow_base64() -> str:
    with open(WORKFLOW_PATH, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# Loaded once at import time; if you edit workflow_api.json you just need to
# restart the bot process to pick up the change.
WORKFLOW_BASE64 = _load_workflow_base64()
logger.info("Loaded workflow_api.json from %s (%d bytes base64)", WORKFLOW_PATH, len(WORKFLOW_BASE64))


@dataclass
class UserSession:
    image_b64: Optional[str] = None
    audio_b64: Optional[str] = None


sessions: Dict[int, UserSession] = {}


def _get_session(user_id: int) -> UserSession:
    if user_id not in sessions:
        sessions[user_id] = UserSession()
    return sessions[user_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bienvenido al bot Wan2.2-S2V (imagen + audio -> video hablando/cantando).\n\n"
        "Pasos:\n"
        "1️⃣ Envía una foto de referencia.\n"
        "2️⃣ Envía un audio o nota de voz.\n"
        "3️⃣ Envía el texto describiendo la acción (o usa /generate <prompt>).\n\n"
        "Cuando tenga imagen + audio + prompt, generaré el video automáticamente.\n"
        "Usa /reset para empezar de nuevo."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("🔄 Sesión reiniciada. Envía una nueva foto para empezar.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = _get_session(update.effective_user.id)
    photo = update.message.photo[-1]
    file = await photo.get_file()
    raw = await file.download_as_bytearray()
    session.image_b64 = base64.b64encode(bytes(raw)).decode("utf-8")
    await update.message.reply_text(
        "🖼️ Imagen recibida. Ahora envía el audio/nota de voz."
        if not session.audio_b64
        else "🖼️ Imagen actualizada. Ya tienes audio guardado; envía el prompt cuando quieras."
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = _get_session(update.effective_user.id)
    media = update.message.voice or update.message.audio
    if media is None:
        return
    file = await media.get_file()
    raw = await file.download_as_bytearray()
    session.audio_b64 = base64.b64encode(bytes(raw)).decode("utf-8")
    await update.message.reply_text(
        "🎵 Audio recibido. Ahora envía el prompt de texto describiendo la escena "
        "(o usa /generate <prompt>)."
        if not session.image_b64
        else "🎵 Audio actualizado. Ya tienes imagen guardada; envía el prompt cuando quieras."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = update.message.text.strip()
    await _run_generation(update, context, prompt)


async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("Uso: /generate <descripción del video>")
        return
    await _run_generation(update, context, prompt)


async def _run_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str):
    session = _get_session(update.effective_user.id)

    if not session.image_b64:
        await update.message.reply_text("⚠️ Primero envía una foto de referencia.")
        return
    if not session.audio_b64:
        await update.message.reply_text("⚠️ Primero envía un audio o nota de voz.")
        return
    if not prompt:
        await update.message.reply_text("⚠️ Necesito un prompt de texto describiendo la escena.")
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)
    status_msg = await update.message.reply_text("🚀 Enviando job a RunPod...")

    job_input = {
        "image_base64": session.image_b64,
        "audio_base64": session.audio_b64,
        "prompt": prompt,
        "use_lightning_lora": USE_LIGHTNING_LORA,
        "width": WIDTH,
        "height": HEIGHT,
        "num_extends": NUM_EXTENDS,
        "workflow_base64": WORKFLOW_BASE64,
    }

    try:
        video_bytes, filename = await _submit_and_wait(job_input, status_msg)
    except asyncio.TimeoutError:
        await status_msg.edit_text("⏱️ Tiempo de espera agotado esperando el resultado.")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error generating video")
        await status_msg.edit_text(f"❌ Error generando el video: {exc}")
        return

    await status_msg.edit_text("✅ Video generado. Subiendo a Telegram...")
    await update.message.reply_video(video=video_bytes, filename=filename, caption=prompt[:1024])

    # Clear session so the next request starts fresh (keeps memory usage bounded)
    sessions.pop(update.effective_user.id, None)


async def _submit_and_wait(job_input: dict, status_msg):
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{RUNPOD_BASE_URL}/run",
            headers=RUNPOD_HEADERS,
            json={"input": job_input},
        )
        resp.raise_for_status()
        job = resp.json()
        job_id = job["id"]

        await status_msg.edit_text(f"⏳ Job en cola (id: {job_id}). Generando video, esto puede tardar varios minutos...")

        elapsed = 0.0
        while elapsed < JOB_TIMEOUT_S:
            await asyncio.sleep(POLL_INTERVAL_S)
            elapsed += POLL_INTERVAL_S

            status_resp = await client.get(
                f"{RUNPOD_BASE_URL}/status/{job_id}",
                headers=RUNPOD_HEADERS,
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
            status = status_data.get("status")

            if status == "COMPLETED":
                output = status_data["output"]
                video_b64 = output["video_base64"]
                filename = output.get("filename", "output.mp4")
                return base64.b64decode(video_b64), filename

            if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
                raise RuntimeError(f"RunPod job {status}: {status_data.get('error', status_data)}")

            # IN_QUEUE / IN_PROGRESS -> keep polling
            if int(elapsed) % 30 == 0:
                await status_msg.edit_text(
                    f"⏳ Job {job_id} sigue en estado '{status}'... ({int(elapsed)}s transcurridos)"
                )

        raise asyncio.TimeoutError()


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("generate", generate_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
