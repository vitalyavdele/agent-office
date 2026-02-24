"""
Telegram bot для Agent Office.

Возможности:
- Текстовые сообщения → задача в оркестратор
- Голосовые сообщения → расшифровка через OpenAI Whisper → задача в оркестратор
- Уведомления в TG: план принят, агент работает, задача выполнена

Переменные окружения:
  TG_BOT_TOKEN         — токен бота (@BotFather)
  TG_ADMIN_CHAT_ID     — ваш личный Telegram user ID (только вы управляете ботом)
  OPENAI_API_KEY       — ключ OpenAI для расшифровки голоса (необязательно)
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

TG_BOT_TOKEN   = os.getenv("TG_BOT_TOKEN", "")
TG_ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Callbacks, injected from main.py
_forward_fn = None   # async (task: str) -> None
_bot_ref    = None   # telegram.Bot instance

# ── Injection ────────────────────────────────────────────────────────────────

def set_forward(fn):
    global _forward_fn
    _forward_fn = fn

def set_bot(bot):
    global _bot_ref
    _bot_ref = bot

# ── Notifications from main → TG ─────────────────────────────────────────────

async def notify(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to the admin chat (called from main.py callbacks)."""
    if not _bot_ref or not TG_ADMIN_CHAT_ID:
        return
    try:
        await _bot_ref.send_message(
            chat_id=TG_ADMIN_CHAT_ID,
            text=text,
            parse_mode=parse_mode,
        )
    except Exception as e:
        logger.warning("[TG Bot] notify error: %s", e)

# ── Whisper transcription ────────────────────────────────────────────────────

async def transcribe_voice(ogg_bytes: bytes) -> str:
    """Transcribe OGG voice using OpenAI Whisper."""
    if not OPENAI_API_KEY:
        logger.warning("[TG Bot] OPENAI_API_KEY not set — voice disabled")
        return ""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": ("voice.ogg", ogg_bytes, "audio/ogg")},
                data={"model": "whisper-1", "language": "ru"},
            )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        logger.warning("[TG Bot] Whisper error %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("[TG Bot] transcribe error: %s", e)
    return ""

# ── Handlers ─────────────────────────────────────────────────────────────────

async def _check_admin(update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if uid != TG_ADMIN_CHAT_ID:
        if update.message:
            await update.message.reply_text("⛔ Нет доступа.")
        return False
    return True


async def cmd_start(update, context):
    if not await _check_admin(update):
        return
    await update.message.reply_text(
        "✦ <b>Agent Office</b>\n\n"
        "Отправь задачу <b>текстом</b> или <b>голосовым</b> — менеджер\n"
        "распределит работу между агентами.\n\n"
        "Обновления будут приходить сюда автоматически.",
        parse_mode="HTML",
    )


async def cmd_status(update, context):
    if not await _check_admin(update):
        return
    await update.message.reply_text("📊 Смотри дашборд — там всё в реальном времени.")


async def handle_text(update, context):
    if not await _check_admin(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    await update.message.reply_text("⏳ Задача принята, передаю менеджеру…")
    if _forward_fn:
        await _forward_fn(text)


async def handle_voice(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("🎙️ Расшифровываю голосовое…")

    voice    = update.message.voice
    tg_file  = await context.bot.get_file(voice.file_id)
    ogg_data = bytes(await tg_file.download_as_bytearray())

    text = await transcribe_voice(ogg_data)
    if not text:
        await msg.edit_text("❌ Не удалось расшифровать. Попробуй отправить текстом.")
        return

    await msg.edit_text(
        f"🎙️ Распознал:\n<i>«{text}»</i>\n\n⏳ Передаю менеджеру…",
        parse_mode="HTML",
    )
    if _forward_fn:
        await _forward_fn(text)


# ── App factory ───────────────────────────────────────────────────────────────

def create_app():
    """Build and return a python-telegram-bot Application, or None if disabled."""
    if not TG_BOT_TOKEN:
        logger.info("[TG Bot] TG_BOT_TOKEN not set — bot disabled")
        return None

    try:
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError:
        logger.warning("[TG Bot] python-telegram-bot not installed — bot disabled")
        return None

    app = Application.builder().token(TG_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    return app
