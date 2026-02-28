"""
Telegram bot для Agent Office.

Возможности:
- Текстовые сообщения → задача в оркестратор
- Голосовые сообщения → расшифровка через OpenAI Whisper → задача в оркестратор
- Уведомления в TG: план принят, агент работает, задача выполнена
- Команды: /tasks, /status, /ideas, /quest, /agents, /errors, /help
- Inline keyboards для управления задачами и квестами
- Rich уведомления с прогрессом агентов

Переменные окружения:
  TG_BOT_TOKEN         — токен бота (@BotFather)
  TG_ADMIN_CHAT_ID     — ваш личный Telegram user ID (только вы управляете ботом)
  OPENAI_API_KEY       — ключ OpenAI для расшифровки голоса (необязательно)
  DASHBOARD_URL        — URL backend API
"""

import io
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_ADMIN_CHAT_ID = int(os.getenv("TG_ADMIN_CHAT_ID", "0"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
BACKEND_URL = os.getenv("DASHBOARD_URL", "https://office.mopofipofue.beget.app")

# Callbacks, injected from main.py
_forward_fn = None   # async (task: str) -> None
_bot_ref = None       # telegram.Bot instance

STATUS_ICONS = {
    "pending": "⏳", "in_progress": "🔄", "done": "✅",
    "cancelled": "❌", "error": "💥", "processing": "⚙️",
}

AGENT_EMOJI = {
    "manager": "🎯", "researcher": "🔍", "writer": "✍️",
    "coder": "💻", "qa": "🛡️", "deployer": "🚀",
}

# ── Injection ────────────────────────────────────────────────────────────────

def set_forward(fn):
    global _forward_fn
    _forward_fn = fn

def set_bot(bot):
    global _bot_ref
    _bot_ref = bot

# ── Internal API calls ───────────────────────────────────────────────────────

async def _api_get(path: str) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(f"{BACKEND_URL}{path}")
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.warning("[TG Bot] API GET %s error: %s", path, e)
        return None

async def _api_post(path: str, data: dict | None = None) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{BACKEND_URL}{path}", json=data or {})
            return r.json() if r.status_code in (200, 201) else None
    except Exception as e:
        logger.warning("[TG Bot] API POST %s error: %s", path, e)
        return None

async def _api_put(path: str, data: dict | None = None) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.put(f"{BACKEND_URL}{path}", json=data or {})
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.warning("[TG Bot] API PUT %s error: %s", path, e)
        return None

# ── Notifications from main → TG ────────────────────────────────────────────

async def notify(text: str, parse_mode: str = "HTML") -> None:
    """Send a message to the admin chat (called from main.py callbacks)."""
    if not _bot_ref or not TG_ADMIN_CHAT_ID:
        return
    try:
        await _bot_ref.send_message(
            chat_id=TG_ADMIN_CHAT_ID,
            text=text[:4096],
            parse_mode=parse_mode,
        )
    except Exception as e:
        logger.warning("[TG Bot] notify error: %s", e)


async def notify_quest(quest: dict) -> None:
    """Send quest notification with inline keyboard."""
    if not _bot_ref or not TG_ADMIN_CHAT_ID:
        return
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        qid = quest.get("id", 0)
        title = quest.get("title", "Квест")
        desc = quest.get("description", "")
        qtype = quest.get("quest_type", "info")
        xp = quest.get("xp_reward", 0)
        data = quest.get("data") or {}

        text = f"⭐ <b>Новый квест</b> (+{xp} XP)\n\n<b>{title}</b>\n{desc}"

        # For quests that need text input, ask for reply
        input_required = data.get("input_required", False)
        if input_required or qtype in ("provide_token", "api_key"):
            label = data.get("input_label", "Введите значение")
            text += f"\n\n💬 <i>Ответь на это сообщение: {label}</i>"
            await _bot_ref.send_message(
                chat_id=TG_ADMIN_CHAT_ID,
                text=text,
                parse_mode="HTML",
            )
        else:
            # Simple approve/info quest with button
            buttons = [[
                InlineKeyboardButton("✅ Выполнить", callback_data=f"quest_ok_{qid}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"quest_skip_{qid}"),
            ]]
            await _bot_ref.send_message(
                chat_id=TG_ADMIN_CHAT_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
    except Exception as e:
        logger.warning("[TG Bot] notify_quest error: %s", e)


async def notify_agent_progress(agent: str, task: str, progress: int) -> None:
    """Send agent progress update."""
    if not _bot_ref or not TG_ADMIN_CHAT_ID:
        return
    emoji = AGENT_EMOJI.get(agent, "🤖")
    short = task[:150] + ("…" if len(task) > 150 else "")
    try:
        await _bot_ref.send_message(
            chat_id=TG_ADMIN_CHAT_ID,
            text=f"{emoji} <b>{agent.capitalize()}</b> работает\n{short}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("[TG Bot] notify_progress error: %s", e)


async def send_result_file(title: str, content: str, fmt: str = "md") -> None:
    """Send task result as a file."""
    if not _bot_ref or not TG_ADMIN_CHAT_ID:
        return
    try:
        safe_title = "".join(c for c in title[:30] if c.isalnum() or c in " -_").strip() or "result"
        file = io.BytesIO(content.encode("utf-8"))
        file.name = f"{safe_title}.{fmt}"
        await _bot_ref.send_document(
            chat_id=TG_ADMIN_CHAT_ID,
            document=file,
            caption=f"📄 Результат: {title[:60]}",
        )
    except Exception as e:
        logger.warning("[TG Bot] send_file error: %s", e)


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

# ── Admin check ──────────────────────────────────────────────────────────────

async def _check_admin(update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    if uid != TG_ADMIN_CHAT_ID:
        if update.message:
            await update.message.reply_text("⛔ Нет доступа.")
        return False
    return True

# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update, context):
    if not await _check_admin(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    buttons = [
        [InlineKeyboardButton("📋 Задачи", callback_data="cmd_tasks"),
         InlineKeyboardButton("📊 Статус", callback_data="cmd_status")],
        [InlineKeyboardButton("⭐ Квесты", callback_data="cmd_quest"),
         InlineKeyboardButton("💡 Идеи", callback_data="cmd_ideas")],
        [InlineKeyboardButton("👥 Агенты", callback_data="cmd_agents"),
         InlineKeyboardButton("❓ Помощь", callback_data="cmd_help")],
    ]
    await update.message.reply_text(
        "✦ <b>Agent Office</b>\n\n"
        "Отправь задачу <b>текстом</b> или <b>голосовым</b> — менеджер\n"
        "распределит работу между агентами.\n\n"
        "Обновления будут приходить сюда автоматически.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cmd_help(update, context):
    if not await _check_admin(update):
        return
    await update.message.reply_text(
        "📖 <b>Команды</b>\n\n"
        "/tasks — список задач\n"
        "/status — статус системы\n"
        "/quest — pending квесты\n"
        "/ideas — идеи\n"
        "/agents — статус агентов\n"
        "/errors — последние ошибки\n"
        "/help — эта справка\n\n"
        "Или просто отправь текст/голосовое — создастся задача.",
        parse_mode="HTML",
    )


async def cmd_status(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю статус…")

    briefing = await _api_get("/api/briefing")
    health = await _api_get("/")

    if not briefing:
        await msg.edit_text("❌ Не удалось получить статус")
        return

    version = health.get("version", "?") if health else "?"
    pending_quests = briefing.get("pending_quests", 0)
    completed_24h = briefing.get("completed_tasks_24h", 0)
    active_agents = briefing.get("active_agents", [])

    agents_str = ", ".join(active_agents) if active_agents else "нет"

    await msg.edit_text(
        f"📊 <b>Статус системы</b>\n\n"
        f"🏷 Версия: <code>{version}</code>\n"
        f"⭐ Pending квестов: {pending_quests}\n"
        f"✅ Задач за 24ч: {completed_24h}\n"
        f"🤖 Активные агенты: {agents_str}",
        parse_mode="HTML",
    )


async def cmd_tasks(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю задачи…")

    data = await _api_get("/api/scheduled-tasks?limit=10")
    if not data:
        await msg.edit_text("❌ Не удалось загрузить задачи")
        return

    tasks = data.get("tasks", [])
    if not tasks:
        await msg.edit_text("📋 Нет задач")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    await msg.delete()
    for t in tasks[:8]:
        icon = STATUS_ICONS.get(t.get("status", ""), "❓")
        title = t.get("title", "Без названия")[:60]
        horizon = t.get("horizon", "")
        priority = t.get("priority", "normal")
        tid = t["id"]

        text = f"{icon} <b>{title}</b>\n{horizon} | {priority}"

        buttons = []
        st = t.get("status", "")
        if st == "pending":
            buttons.append([
                InlineKeyboardButton("▶️ Запустить", callback_data=f"run_{tid}"),
                InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{tid}"),
            ])
        elif st == "done" and t.get("linked_task_id"):
            buttons.append([
                InlineKeyboardButton("📋 Результат", callback_data=f"result_{tid}"),
            ])

        await _bot_ref.send_message(
            chat_id=TG_ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )


async def cmd_quest(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю квесты…")

    data = await _api_get("/api/quests?status=pending&limit=10")
    if not data:
        await msg.edit_text("❌ Не удалось загрузить квесты")
        return

    quests = data.get("quests", [])
    if not quests:
        await msg.edit_text("⭐ Нет pending квестов")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    await msg.delete()
    for q in quests[:5]:
        qid = q["id"]
        title = q.get("title", "Квест")
        desc = q.get("description", "")[:200]
        xp = q.get("xp_reward", 0)

        text = f"⭐ <b>{title}</b> (+{xp} XP)\n{desc}"

        buttons = [[
            InlineKeyboardButton("✅ Выполнить", callback_data=f"quest_ok_{qid}"),
        ]]
        await _bot_ref.send_message(
            chat_id=TG_ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def cmd_ideas(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю идеи…")

    data = await _api_get("/api/ideas")
    if not data:
        await msg.edit_text("❌ Не удалось загрузить идеи")
        return

    ideas = data.get("ideas", [])
    if not ideas:
        await msg.edit_text("💡 Нет идей")
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    await msg.delete()
    for idea in ideas[:5]:
        iid = idea["id"]
        content = idea.get("content", "")[:100]
        st = idea.get("status", "planning")
        icon = {"planning": "🔮", "planned": "📝", "active": "🚀", "done": "✅"}.get(st, "💡")

        text = f"{icon} <b>{st}</b>\n{content}"
        buttons = []
        if st in ("planning", "planned"):
            buttons.append([
                InlineKeyboardButton("🚀 Запустить", callback_data=f"idea_start_{iid}"),
            ])
        await _bot_ref.send_message(
            chat_id=TG_ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )


async def cmd_agents(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю агентов…")

    data = await _api_get("/api/agents/stats")
    if not data:
        await msg.edit_text("❌ Не удалось загрузить агентов")
        return

    agents = data.get("agents", [])
    lines = []
    for a in agents:
        emoji = AGENT_EMOJI.get(a.get("agent", ""), "🤖")
        name = a.get("agent", "?").capitalize()
        tasks_count = a.get("tasks_count", 0)
        avg_rating = a.get("avg_rating")
        rating_str = f"⭐{avg_rating:.1f}" if avg_rating else "—"
        errors = a.get("error_count", 0)
        lines.append(f"{emoji} <b>{name}</b>: {tasks_count} задач, {rating_str}, {errors} ошибок")

    await msg.edit_text(
        "👥 <b>Команда агентов</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


async def cmd_errors(update, context):
    if not await _check_admin(update):
        return
    msg = await update.message.reply_text("⏳ Загружаю ошибки…")

    data = await _api_get("/api/errors?limit=5")
    if not data:
        await msg.edit_text("❌ Не удалось загрузить ошибки")
        return

    errors = data.get("errors", [])
    if not errors:
        await msg.edit_text("🎉 Ошибок нет!")
        return

    lines = []
    for e in errors[:5]:
        emoji = AGENT_EMOJI.get(e.get("agent", ""), "🤖")
        agent = e.get("agent", "?")
        etype = e.get("error_type", "unknown")
        detail = e.get("error_detail", "")[:80]
        reflected = "✅" if e.get("reflection") else "❌"
        lines.append(f"{emoji} <b>{agent}</b> | {etype} | рефлексия: {reflected}\n<i>{detail}</i>")

    await msg.edit_text(
        "💥 <b>Последние ошибки</b>\n\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )


# ── Callback query handler (inline buttons) ─────────────────────────────────

async def handle_callback(update, context):
    query = update.callback_query
    if not query:
        return
    uid = query.from_user.id if query.from_user else 0
    if uid != TG_ADMIN_CHAT_ID:
        await query.answer("⛔ Нет доступа", show_alert=True)
        return

    await query.answer()
    data = query.data or ""

    # ── Task actions ──
    if data.startswith("run_"):
        tid = data.split("_", 1)[1]
        result = await _api_post(f"/api/scheduled-tasks/{tid}/run")
        if result and result.get("ok"):
            await query.edit_message_text("✅ Задача запущена!")
        else:
            await query.edit_message_text("❌ Не удалось запустить задачу")

    elif data.startswith("cancel_"):
        tid = data.split("_", 1)[1]
        result = await _api_put(f"/api/scheduled-tasks/{tid}/status", {"status": "cancelled"})
        if result:
            await query.edit_message_text("❌ Задача отменена")
        else:
            await query.edit_message_text("❌ Не удалось отменить")

    elif data.startswith("result_"):
        tid = data.split("_", 1)[1]
        detail = await _api_get(f"/api/tasks/{tid}/detail")
        if detail and detail.get("task", {}).get("result"):
            result_text = detail["task"]["result"]
            # Try to parse structured result
            try:
                parsed = json.loads(result_text)
                title = parsed.get("title", "Результат")
                summary = parsed.get("summary", "")
                text = f"📋 <b>{title}</b>\n\n{summary[:500]}"
            except (json.JSONDecodeError, TypeError):
                text = f"📋 <b>Результат</b>\n\n{result_text[:500]}"
            await query.edit_message_text(text, parse_mode="HTML")
        else:
            await query.edit_message_text("❌ Результат не найден")

    # ── Quest actions ──
    elif data.startswith("quest_ok_"):
        qid = data.split("_", 2)[2]
        result = await _api_put(f"/api/quests/{qid}/complete", {"response": "approved"})
        if result:
            await query.edit_message_text("✅ Квест выполнен!")
        else:
            await query.edit_message_text("❌ Не удалось выполнить квест")

    elif data.startswith("quest_skip_"):
        qid = data.split("_", 2)[2]
        await query.edit_message_text("⏭ Квест пропущен")

    # ── Idea actions ──
    elif data.startswith("idea_start_"):
        iid = data.split("_", 2)[2]
        result = await _api_post(f"/api/ideas/{iid}/start")
        if result:
            await query.edit_message_text("🚀 Идея запущена в работу!")
        else:
            await query.edit_message_text("❌ Не удалось запустить идею")

    # ── Menu commands from /start buttons ──
    elif data == "cmd_tasks":
        await _inline_tasks(query)
    elif data == "cmd_status":
        await _inline_status(query)
    elif data == "cmd_quest":
        await _inline_quest(query)
    elif data == "cmd_ideas":
        await _inline_ideas(query)
    elif data == "cmd_agents":
        await _inline_agents(query)
    elif data == "cmd_help":
        await query.edit_message_text(
            "📖 <b>Команды</b>\n\n"
            "/tasks — список задач\n"
            "/status — статус системы\n"
            "/quest — pending квесты\n"
            "/ideas — идеи\n"
            "/agents — статус агентов\n"
            "/errors — последние ошибки\n"
            "/help — эта справка\n\n"
            "Или просто отправь текст/голосовое — создастся задача.",
            parse_mode="HTML",
        )


# ── Inline versions of commands (called from /start menu) ───────────────────

async def _inline_tasks(query):
    data = await _api_get("/api/scheduled-tasks?limit=5")
    tasks = (data or {}).get("tasks", [])
    if not tasks:
        await query.edit_message_text("📋 Нет задач")
        return
    lines = []
    for t in tasks[:5]:
        icon = STATUS_ICONS.get(t.get("status", ""), "❓")
        title = t.get("title", "")[:50]
        lines.append(f"{icon} {title}")
    await query.edit_message_text(
        "📋 <b>Задачи</b>\n\n" + "\n".join(lines) + "\n\n/tasks — подробнее",
        parse_mode="HTML",
    )

async def _inline_status(query):
    briefing = await _api_get("/api/briefing")
    if not briefing:
        await query.edit_message_text("❌ Не удалось получить статус")
        return
    pending = briefing.get("pending_quests", 0)
    done = briefing.get("completed_tasks_24h", 0)
    await query.edit_message_text(
        f"📊 <b>Статус</b>\n\n⭐ Квестов: {pending}\n✅ Задач за 24ч: {done}",
        parse_mode="HTML",
    )

async def _inline_quest(query):
    data = await _api_get("/api/quests?status=pending&limit=3")
    quests = (data or {}).get("quests", [])
    if not quests:
        await query.edit_message_text("⭐ Нет pending квестов")
        return
    lines = [f"⭐ {q.get('title', '?')}" for q in quests[:3]]
    await query.edit_message_text(
        "⭐ <b>Квесты</b>\n\n" + "\n".join(lines) + "\n\n/quest — подробнее",
        parse_mode="HTML",
    )

async def _inline_ideas(query):
    data = await _api_get("/api/ideas")
    ideas = (data or {}).get("ideas", [])
    if not ideas:
        await query.edit_message_text("💡 Нет идей")
        return
    lines = [f"💡 {i.get('content', '?')[:50]}" for i in ideas[:3]]
    await query.edit_message_text(
        "💡 <b>Идеи</b>\n\n" + "\n".join(lines) + "\n\n/ideas — подробнее",
        parse_mode="HTML",
    )

async def _inline_agents(query):
    data = await _api_get("/api/agents/stats")
    agents = (data or {}).get("agents", [])
    lines = []
    for a in agents:
        emoji = AGENT_EMOJI.get(a.get("agent", ""), "🤖")
        name = a.get("agent", "?").capitalize()
        lines.append(f"{emoji} {name}: {a.get('tasks_count', 0)} задач")
    await query.edit_message_text(
        "👥 <b>Агенты</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
    )


# ── Message handlers ─────────────────────────────────────────────────────────

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

    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)
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


# ── App factory ──────────────────────────────────────────────────────────────

def create_app():
    """Build and return a python-telegram-bot Application, or None if disabled."""
    if not TG_BOT_TOKEN:
        logger.info("[TG Bot] TG_BOT_TOKEN not set — bot disabled")
        return None

    try:
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError:
        logger.warning("[TG Bot] python-telegram-bot not installed — bot disabled")
        return None

    app = Application.builder().token(TG_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("quest", cmd_quest))
    app.add_handler(CommandHandler("ideas", cmd_ideas))
    app.add_handler(CommandHandler("agents", cmd_agents))
    app.add_handler(CommandHandler("errors", cmd_errors))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    return app
