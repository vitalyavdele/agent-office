"""
Personal planner for Agent Office TG bot.

Flow:
  - Каждый вечер в PLANNER_EVENING_HOUR бот спрашивает что планируешь на завтра
  - Пользователь отвечает текстом или голосом (любые форматы)
  - GPT разбирает на задачи с временем
  - Утром в PLANNER_MORNING_HOUR — дайджест дня
  - За PLANNER_REMINDER_MINS минут до задачи — напоминание

Переменные окружения (необязательные, есть дефолты):
  PLANNER_TZ              — часовой пояс (default: Europe/Moscow)
  PLANNER_EVENING_HOUR    — час вечернего опроса (default: 21)
  PLANNER_MORNING_HOUR    — час утреннего дайджеста (default: 9)
  PLANNER_REMINDER_MINS   — минут до события для напоминания (default: 30)

Хранилище: SQLite (planner.db в папке проекта)
"""

import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

TZ_STR         = os.getenv("PLANNER_TZ",            "Europe/Moscow")
EVENING_HOUR   = int(os.getenv("PLANNER_EVENING_HOUR",  "21"))
MORNING_HOUR   = int(os.getenv("PLANNER_MORNING_HOUR",  "9"))
REMINDER_MINS  = int(os.getenv("PLANNER_REMINDER_MINS", "30"))
OPENAI_KEY     = os.getenv("OPENAI_API_KEY", "")

TZ = ZoneInfo(TZ_STR)

DB_PATH = Path(__file__).parent / "planner.db"

# ── State ─────────────────────────────────────────────────────────────────────
# Single-user bot, so module-level state is fine.
_awaiting_plan: bool = False
_plan_for_date: date | None = None


def set_awaiting_plan(target_date: date | None = None) -> None:
    global _awaiting_plan, _plan_for_date
    _awaiting_plan = True
    _plan_for_date = target_date or (now().date() + timedelta(days=1))


def clear_awaiting_plan() -> None:
    global _awaiting_plan, _plan_for_date
    _awaiting_plan = False
    _plan_for_date = None


def is_awaiting_plan() -> bool:
    return _awaiting_plan


def get_plan_date() -> date:
    return _plan_for_date or (now().date() + timedelta(days=1))


def now() -> datetime:
    return datetime.now(TZ)


# ── Database ──────────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planner_tasks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_date   TEXT NOT NULL,
            task_time   TEXT,
            title       TEXT NOT NULL,
            is_done     INTEGER DEFAULT 0,
            reminded    INTEGER DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def save_tasks(tasks: list[dict], target_date: date) -> list[dict]:
    conn = _db()
    saved = []
    date_str = target_date.isoformat()
    # Clear existing non-done tasks for that date to avoid duplicates on re-planning
    conn.execute(
        "DELETE FROM planner_tasks WHERE task_date = ? AND is_done = 0",
        (date_str,)
    )
    for t in tasks:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        cur = conn.execute(
            "INSERT INTO planner_tasks (task_date, task_time, title) VALUES (?, ?, ?)",
            (date_str, t.get("time"), title),
        )
        saved.append({"id": cur.lastrowid, "time": t.get("time"), "title": title})
    conn.commit()
    conn.close()
    return saved


def get_tasks(target_date: date) -> list[dict]:
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM planner_tasks WHERE task_date = ? ORDER BY task_time, id",
        (target_date.isoformat(),),
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    # Sort: timed tasks first (sorted), untimed at end
    timed   = sorted([r for r in result if r["task_time"]], key=lambda x: x["task_time"])
    untimed = [r for r in result if not r["task_time"]]
    return timed + untimed


def mark_done(task_id: int) -> bool:
    conn = _db()
    cur = conn.execute("UPDATE planner_tasks SET is_done = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_pending_reminders(target_date: date) -> list[dict]:
    """Tasks with time, not done, not yet reminded."""
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM planner_tasks WHERE task_date = ? AND task_time IS NOT NULL "
        "AND is_done = 0 AND reminded = 0 ORDER BY task_time",
        (target_date.isoformat(),),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_reminded(task_id: int) -> None:
    conn = _db()
    conn.execute("UPDATE planner_tasks SET reminded = 1 WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()


# ── AI Parsing ────────────────────────────────────────────────────────────────

DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


async def parse_tasks(text: str, target_date: date) -> list[dict]:
    """Parse natural-language task list into [{time, title}] dicts."""
    if OPENAI_KEY:
        result = await _parse_with_gpt(text, target_date)
        if result:
            return result
    return _parse_simple(text)


async def _parse_with_gpt(text: str, target_date: date) -> list[dict]:
    day_name = DAYS_RU[target_date.weekday()]
    prompt = (
        f"Ты парсер задач. Распарси список дел из сообщения пользователя.\n"
        f"Дата: {target_date.strftime('%d.%m.%Y')} ({day_name})\n\n"
        f"Правила:\n"
        f"- time: время в формате HH:MM, или null если не указано\n"
        f"- title: краткое название задачи на русском\n"
        f"- Если написано 'утром' — 09:00, 'в обед' — 13:00, 'вечером' — 19:00, 'ночью' — 22:00\n\n"
        f"Верни JSON: {{\"tasks\": [{{\"time\": \"HH:MM\" или null, \"title\": \"...\"}}]}}\n\n"
        f"Сообщение: {text}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 600,
                },
            )
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content).get("tasks", [])
    except Exception as e:
        logger.warning("[Planner] GPT parse error: %s", e)
    return []


def _parse_simple(text: str) -> list[dict]:
    """Fallback: regex-based parsing."""
    time_re = re.compile(
        r'\b(\d{1,2})[:\.](\d{2})\b'          # 09:00, 9.00
        r'|\b(\d{1,2})\s*(?:час|утра|дня|вечера|ночи)\b',  # 9 вечера
        re.IGNORECASE,
    )
    tasks = []
    for line in re.split(r'[\n;,]', text):
        line = line.strip().lstrip('-–•*·0123456789). ')
        if len(line) < 3:
            continue
        m = time_re.search(line)
        t = None
        if m:
            if m.group(1) and m.group(2):
                t = f"{int(m.group(1)):02d}:{m.group(2)}"
            line = time_re.sub('', line).strip().lstrip('— -')
        if line:
            tasks.append({"time": t, "title": line})
    return tasks


# ── Formatting ────────────────────────────────────────────────────────────────

def format_day(tasks: list[dict], date_label: str = "") -> str:
    if not tasks:
        return "Задач нет. 🎉"

    lines = []
    if date_label:
        lines.append(f"<b>{date_label}</b>\n")

    timed   = [t for t in tasks if t.get("task_time")]
    untimed = [t for t in tasks if not t.get("task_time")]

    for t in timed:
        icon  = "✅" if t.get("is_done") else "🕐"
        title = f"<s>{t['title']}</s>" if t.get("is_done") else t["title"]
        lines.append(f"{icon} <b>{t['task_time']}</b> — {title}")

    if untimed:
        if timed:
            lines.append("")
        for t in untimed:
            icon  = "✅" if t.get("is_done") else "📌"
            title = f"<s>{t['title']}</s>" if t.get("is_done") else t["title"]
            lines.append(f"{icon} {title}")

    return "\n".join(lines)


def date_label(d: date) -> str:
    day_name = DAYS_RU[d.weekday()]
    today = now().date()
    if d == today:
        prefix = "Сегодня"
    elif d == today + timedelta(days=1):
        prefix = "Завтра"
    else:
        prefix = ""
    return f"{prefix} {day_name}, {d.strftime('%d.%m.%Y')}".strip()


# ── Scheduled job callbacks ───────────────────────────────────────────────────

async def job_evening_checkin(context) -> None:
    """Ask user for tomorrow's plan."""
    from tg_bot import TG_ADMIN_CHAT_ID
    chat_id = TG_ADMIN_CHAT_ID
    if not chat_id:
        return

    tomorrow = now().date() + timedelta(days=1)
    day_name  = DAYS_RU[tomorrow.weekday()]

    set_awaiting_plan(tomorrow)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🌙 <b>Планируем {day_name}, {tomorrow.strftime('%d.%m')}?</b>\n\n"
            f"Перечисли задачи на завтра — с временем если важно.\n"
            f"Например: <i>«в 9 звонок врачу, в 14 встреча, вечером тренировка»</i>"
        ),
        parse_mode="HTML",
    )


async def job_morning_digest(context) -> None:
    """Send today's plan as morning digest."""
    from tg_bot import TG_ADMIN_CHAT_ID
    chat_id = TG_ADMIN_CHAT_ID
    if not chat_id:
        return

    today  = now().date()
    tasks  = get_tasks(today)
    label  = date_label(today)

    if not tasks:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"☀️ Доброе утро! На <b>{label}</b> задач нет.\n\n"
                 f"Добавить: /plan",
            parse_mode="HTML",
        )
        return

    text = f"☀️ Доброе утро!\n\n{format_day(tasks, label)}\n\n/done &lt;номер&gt; — отметить выполненным"
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    # Schedule today's reminders
    schedule_reminders(context.application, chat_id, tasks, today)


async def job_reminder(context) -> None:
    """Fire a reminder for a specific task."""
    data    = context.job.data
    chat_id = data["chat_id"]
    task    = data["task"]
    mark_reminded(task["id"])
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏰ <b>Через {REMINDER_MINS} мин:</b> {task['title']} ({task['task_time']})",
        parse_mode="HTML",
    )


# ── Reminder scheduling ───────────────────────────────────────────────────────

def schedule_reminders(application, chat_id: int, tasks: list[dict], target_date: date) -> int:
    """Schedule job_reminder for each task that hasn't been reminded yet."""
    scheduled = 0
    current   = now()

    for task in tasks:
        if not task.get("task_time") or task.get("is_done") or task.get("reminded"):
            continue
        try:
            h, m   = map(int, task["task_time"].split(":"))
            task_dt = datetime.combine(target_date, time(h, m), tzinfo=TZ)
            remind_dt = task_dt - timedelta(minutes=REMINDER_MINS)
            if remind_dt <= current:
                continue
            application.job_queue.run_once(
                callback=job_reminder,
                when=remind_dt,
                data={"chat_id": chat_id, "task": task},
                name=f"remind_{task['id']}",
            )
            scheduled += 1
        except Exception as e:
            logger.warning("[Planner] schedule reminder error for task %s: %s", task.get("id"), e)
    return scheduled


def register_daily_jobs(application) -> None:
    """Register recurring daily jobs. Called once at bot startup."""
    from datetime import time as dt_time

    jq = application.job_queue

    # Evening check-in
    jq.run_daily(
        callback=job_evening_checkin,
        time=dt_time(EVENING_HOUR, 0, tzinfo=TZ),
        name="planner_evening",
    )
    # Morning digest
    jq.run_daily(
        callback=job_morning_digest,
        time=dt_time(MORNING_HOUR, 0, tzinfo=TZ),
        name="planner_morning",
    )
    logger.info(
        "[Planner] Daily jobs registered: evening=%d:00, morning=%d:00 (%s)",
        EVENING_HOUR, MORNING_HOUR, TZ_STR,
    )


def reschedule_todays_reminders(application, chat_id: int) -> None:
    """Called at startup to reschedule any pending reminders for today."""
    today  = now().date()
    tasks  = get_pending_reminders(today)
    n      = schedule_reminders(application, chat_id, tasks, today)
    if n:
        logger.info("[Planner] Rescheduled %d reminders for today", n)
