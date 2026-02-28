import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("agent-office")

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response

from datetime import datetime, timedelta
from agents import StateManager, AGENT_DEFS
import tg_bot
from monitor import SystemMonitor


# ── Structured result builder ─────────────────────────────────────────────────

AGENT_COST_ESTIMATES = {
    "researcher": {"tokens_in": 4000, "tokens_out": 2000, "time_sec": 30},
    "writer":     {"tokens_in": 5000, "tokens_out": 4000, "time_sec": 45},
    "coder":      {"tokens_in": 6000, "tokens_out": 8000, "time_sec": 60},
    "qa":         {"tokens_in": 4000, "tokens_out": 2000, "time_sec": 30},
    "deployer":   {"tokens_in": 0,    "tokens_out": 0,    "time_sec": 30},
    "manager":    {"tokens_in": 3000, "tokens_out": 1500, "time_sec": 20},
}

AGENT_SECTION_NAMES = {
    "researcher": "Исследование и анализ",
    "writer":     "Контент / Спецификация",
    "coder":      "Код и реализация",
    "qa":         "Контроль качества",
    "deployer":   "Деплой и публикация",
}


def _build_structured_result(
    task_title: str,
    manager_summary: str,
    worker_results: list[dict],
    user_actions: list[str] | None = None,
) -> str:
    """Build structured JSON result for quest-style rendering in dashboard."""
    steps = []
    for r in worker_results:
        agent = r.get("agent", "unknown")
        est = AGENT_COST_ESTIMATES.get(agent, {})
        steps.append({
            "agent": agent,
            "label": AGENT_SECTION_NAMES.get(agent, agent.capitalize()),
            "result": r.get("result", ""),
            "tokens_in": est.get("tokens_in", 0),
            "tokens_out": est.get("tokens_out", 0),
            "time_sec": est.get("time_sec", 0),
        })

    all_agents = ["manager"] + [s["agent"] for s in steps]
    total_cost = sum(
        (AGENT_COST_ESTIMATES.get(a, {}).get("tokens_in", 0) * 3
         + AGENT_COST_ESTIMATES.get(a, {}).get("tokens_out", 0) * 15) / 1_000_000
        for a in all_agents
    )
    mgr = AGENT_COST_ESTIMATES["manager"]
    total_tokens = sum(s["tokens_in"] + s["tokens_out"] for s in steps) + mgr["tokens_in"] + mgr["tokens_out"]
    total_time = sum(s["time_sec"] for s in steps) + mgr["time_sec"]

    # user_actions come directly from Manager's plan via n8n callback
    if not user_actions:
        user_actions = []

    # If no summary from Manager, generate one
    if not manager_summary and worker_results:
        agents_used = [AGENT_SECTION_NAMES.get(r["agent"], r["agent"]) for r in worker_results if r.get("agent") != "qa"]
        manager_summary = f"Задача выполнена. Агенты: {', '.join(agents_used)}."

    structured = {
        "version": 2,
        "title": task_title,
        "summary": manager_summary,
        "steps": steps,
        "user_actions": user_actions,
        "metrics": {
            "total_cost": round(total_cost, 4),
            "total_tokens": total_tokens,
            "total_time_sec": total_time,
            "agents_count": len(all_agents),
        },
    }
    return json.dumps(structured, ensure_ascii=False)


async def _get_current_task_title() -> str:
    """Get the title of the currently executing scheduled task."""
    if not state.db or not state._current_task_id:
        return "Результат задачи"
    try:
        rows = await state.db.select("scheduled_tasks", {
            "linked_task_id": f"eq.{state._current_task_id}",
            "limit": "1",
        })
        if rows:
            return rows[0].get("title", "Результат задачи")
    except Exception:
        pass
    return "Результат задачи"


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

# ── State ─────────────────────────────────────────────────────────────────────

state = StateManager(
    supabase_url=os.getenv("SUPABASE_URL", ""),
    supabase_key=os.getenv("SUPABASE_ANON_KEY", ""),
)

N8N_MANAGER_WEBHOOK = os.getenv("N8N_MANAGER_WEBHOOK", "")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")
clients: set[WebSocket] = set()

# Accumulate worker results during a task execution
_task_results: list[dict] = []  # [{agent, result, timestamp}]

# ── Lifespan: start/stop TG bot alongside FastAPI ────────────────────────────

_tg_app = None
_monitor = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tg_app, _monitor
    await state.load_history()

    _tg_app = tg_bot.create_app()
    if _tg_app:
        tg_bot.set_forward(_forward_to_n8n)
        await _tg_app.initialize()
        await _tg_app.start()
        await _tg_app.updater.start_polling(drop_pending_updates=True)
        tg_bot.set_bot(_tg_app.bot)

    asyncio.create_task(_task_timeout_checker())

    # Start system monitor
    _monitor = SystemMonitor(state, broadcast, tg_bot.notify, N8N_MANAGER_WEBHOOK)
    await _monitor.start()

    yield  # ── server running ──

    if _monitor:
        _monitor.stop()
    if _tg_app:
        await _tg_app.updater.stop()
        await _tg_app.stop()
        await _tg_app.shutdown()


APP_VERSION = "4.0.0-ai-office"
_startup_time = datetime.utcnow()

app = FastAPI(lifespan=lifespan, version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://dash.mopofipofue.beget.app",
        "http://85.198.64.70:3000",
        "http://85.198.64.70",
    ],
    allow_origin_regex=r"https://.*\.(vercel\.app|beget\.app)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health check ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"status": "ok", "version": APP_VERSION}

# ── Broadcast to all WS clients ───────────────────────────────────────────────

async def broadcast(event: dict):
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)



# ── WebSocket — browser ↔ dashboard ──────────────────────────────────────────

@app.websocket("/ws")
async def ws_handler(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)

    await websocket.send_json({
        "type":    "init",
        "agents":  state.agent_states(),
        "history": state.history[-80:],
    })

    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "task":
                content = data.get("content", "").strip()
                if content:
                    msg = state.add_user_message(content)
                    await broadcast({"type": "chat", "message": msg})
                    await _forward_to_n8n(content)
    except WebSocketDisconnect:
        clients.discard(websocket)
    except Exception:
        clients.discard(websocket)


# ── REST: receive task (from browser or external) ────────────────────────────

@app.post("/api/task")
async def api_task(request: Request):
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "empty content"}, status_code=400)
    if len(content) > 5000:
        return JSONResponse({"ok": False, "error": "content too long (max 5000)"}, status_code=400)

    msg = state.add_user_message(content)
    await broadcast({"type": "chat", "message": msg})
    await _forward_to_n8n(content)
    return JSONResponse({"ok": True})


# ── REST: n8n → dashboard callbacks ──────────────────────────────────────────

@app.post("/api/n8n/callback")
async def n8n_callback(request: Request):
    """
    n8n workflow POSTs here to update agent status and optionally send a chat message.

    Expected JSON body:
      {
        "agent":    "manager|researcher|writer|coder|analyst",
        "status":   "idle|thinking|working|done",
        "task":     "Current task description",   // optional
        "progress": 0-100,                         // optional
        "message":  "Chat text to display"         // optional
      }
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    await state.apply_callback(broadcast, payload)
    await _maybe_notify_tg(payload)

    agent = payload.get("agent", "")
    message = payload.get("message", "").strip()

    # Log to diary
    if agent and message:
        asyncio.create_task(
            state.add_diary_entry(agent, "status_change", message)
        )

    # Save lessons from worker responses
    lessons = payload.get("lessons_learned")
    if lessons and agent:
        if isinstance(lessons, str):
            lessons = [lessons]
        for lesson in lessons:
            if isinstance(lesson, str) and lesson.strip():
                asyncio.create_task(
                    state.save_memory(
                        agent=agent,
                        memory_type="lesson",
                        content=lesson.strip(),
                        source_task_id=state._current_task_id,
                        importance=6,
                        tags=["auto_learned"],
                    )
                )

    # Save errors from worker responses
    error_detail = payload.get("error_detail")
    if error_detail and agent:
        err = await state.save_error(
            agent=agent,
            error_type=payload.get("error_type", "runtime"),
            error_detail=error_detail,
            task_id=state._current_task_id,
        )
        # Auto-reflect on errors
        if err:
            asyncio.create_task(_auto_reflect_error(err["id"]))

    # Auto-create quest when agent requests it
    if payload.get("status") == "quest":
        raw_qt = payload.get("quest_type", "info")
        safe_qt = raw_qt if raw_qt in VALID_QUEST_TYPES else "info"
        quest = await state.create_quest(
            title=payload.get("quest_title", payload.get("task", "Quest")),
            description=message or "",
            quest_type=safe_qt,
            agent=agent,
            xp_reward=_safe_int(payload.get("xp_reward", 10), 10),
            data=payload.get("quest_data"),
        )
        if quest:
            await broadcast({"type": "quest_created", "quest": quest})
            asyncio.create_task(tg_bot.notify_quest(quest))

    # Accumulate worker results when status=done
    if agent and agent != "manager" and payload.get("status") == "done" and message:
        # Дедупликация: n8n может отправить callback дважды
        is_dup = any(r["agent"] == agent and r["result"] == message for r in _task_results)
        if not is_dup:
            _task_results.append({
                "agent": agent,
                "result": message,
                "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
            })

    # When manager starts thinking — reset accumulator
    if agent == "manager" and payload.get("status") == "thinking":
        _task_results.clear()

    # When manager goes idle (task complete) — build structured result + link + notify
    if agent == "manager" and payload.get("status") == "idle":
        logger.info(f"[idle] Manager idle. task_results={len(_task_results)}, message_len={len(message)}")
        # Title and user_actions come from n8n Parse Plan via callback
        plan_title = payload.get("title", "").strip()
        task_title = plan_title or await _get_current_task_title()
        logger.info(f"[idle] title={task_title}, plan_title={plan_title}")

        # Parse user_actions (JSON string from n8n)
        raw_ua = payload.get("user_actions", "[]")
        try:
            user_actions = json.loads(raw_ua) if isinstance(raw_ua, str) else raw_ua
        except (json.JSONDecodeError, TypeError):
            user_actions = []

        combined = _build_structured_result(
            task_title=task_title,
            manager_summary=message or "",
            worker_results=list(_task_results),
            user_actions=user_actions,
        )
        logger.info(f"[idle] combined_len={len(combined)}, user_actions={user_actions}")
        if combined.strip():
            # ВАЖНО: сначала link (ставит review_status=pending_review),
            # потом notify (ищет pending_review для создания квестов).
            # НЕ параллельно — иначе race condition.
            logger.info("[idle] Linking result to scheduled task...")
            await _link_result_to_scheduled_task(combined)
            logger.info("[idle] Creating quests...")
            await _notify_user_task_done(combined)
            logger.info("[idle] Done.")
        _task_results.clear()

    return JSONResponse({"ok": True})


# ── REST: task history ────────────────────────────────────────────────────────

@app.get("/api/tasks")
async def api_tasks():
    tasks = await state.get_tasks(limit=50)
    return JSONResponse({"tasks": tasks})


@app.get("/api/agent-tasks/{task_id}")
async def api_agent_task_detail(task_id: int):
    task = await state.get_agent_task_by_id(task_id)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"task": task})


# ── REST: diary ───────────────────────────────────────────────────────────────

@app.get("/api/diary")
async def api_diary(agent: str = "", limit: int = 50):
    entries = await state.get_diary(agent=agent or None, limit=min(limit, 200))
    return JSONResponse({"diary": entries})


# ── REST: scheduled tasks ────────────────────────────────────────────────────

VALID_HORIZONS   = {"now", "day", "week", "month"}
VALID_PRIORITIES = {"urgent", "normal", "later"}
VALID_STATUSES   = {"pending", "in_progress", "done", "cancelled"}


@app.post("/api/scheduled-tasks")
async def api_create_scheduled_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    title    = (body.get("title") or "").strip()
    horizon  = body.get("horizon", "now")
    priority = body.get("priority", "normal")

    if not title:
        return JSONResponse({"ok": False, "error": "empty title"}, status_code=400)
    if horizon not in VALID_HORIZONS:
        return JSONResponse({"ok": False, "error": f"invalid horizon, use: {VALID_HORIZONS}"}, status_code=400)
    if priority not in VALID_PRIORITIES:
        return JSONResponse({"ok": False, "error": f"invalid priority, use: {VALID_PRIORITIES}"}, status_code=400)

    task = await state.create_scheduled_task(title, horizon, priority)
    if not task:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True, "task": task})


@app.get("/api/scheduled-tasks")
async def api_list_scheduled_tasks(horizon: str = "", status: str = "", limit: int = 50):
    tasks = await state.get_scheduled_tasks(
        horizon=horizon or None,
        status=status or None,
        limit=min(limit, 200),
    )
    return JSONResponse({"tasks": tasks})


@app.post("/api/scheduled-tasks/{task_id}/run")
async def api_run_scheduled_task(task_id: int):
    """Launch a scheduled task — send it to n8n Manager and link result back."""
    if not state.db:
        return JSONResponse({"ok": False, "error": "no db"}, status_code=500)

    # Get the scheduled task
    tasks = await state.db.select("scheduled_tasks", {"id": f"eq.{task_id}", "limit": "1"})
    if not tasks:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    scheduled = tasks[0]

    if scheduled["status"] == "done":
        return JSONResponse({"ok": False, "error": "task already done"}, status_code=400)

    # Create pipeline task in `tasks` table
    pipeline_task_id = await state.save_task(scheduled["title"])
    if not pipeline_task_id:
        return JSONResponse({"ok": False, "error": "failed to create pipeline task"}, status_code=500)

    # Link scheduled_task ↔ pipeline task and set in_progress
    await state.update_scheduled_task(task_id, {
        "status": "in_progress",
        "linked_task_id": pipeline_task_id,
        "assigned_agent": "manager",
    })

    # Send to n8n
    state._current_task_id = pipeline_task_id
    await broadcast({"type": "tasks_update"})
    asyncio.create_task(_call_n8n(scheduled["title"], pipeline_task_id))

    return JSONResponse({"ok": True, "pipeline_task_id": pipeline_task_id})


@app.put("/api/scheduled-tasks/{task_id}/status")
async def api_update_scheduled_task_status(task_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    new_status = body.get("status", "")
    if new_status not in VALID_STATUSES:
        return JSONResponse({"ok": False, "error": f"invalid status, use: {VALID_STATUSES}"}, status_code=400)

    ok = await state.update_scheduled_task_status(task_id, new_status)
    if not ok:
        return JSONResponse({"ok": False, "error": "db error or not found"}, status_code=500)
    return JSONResponse({"ok": True})


# ── REST: task lifecycle (detail, feedback, reopen) ──────────────────────────

@app.get("/api/tasks/{task_id}/detail")
async def api_task_detail(task_id: int):
    task = await state.get_scheduled_task_by_id(task_id)
    if not task:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Get linked agent logs from tasks table
    agent_logs = []
    if task.get("linked_task_id") and state.db:
        agent_logs = await state.db.select("tasks", {
            "select": "id,content,status,summary,assigned_agent,created_at,finished_at",
            "id": f"eq.{task['linked_task_id']}",
        })

    # Get feedback for this task
    feedback = []
    if state.db:
        feedback = await state.db.select("task_feedback", {
            "task_id": f"eq.{task_id}",
            "order": "created_at.desc",
        })

    # Get timeline from messages (partial title match)
    timeline = []
    title_prefix = (task.get("title") or "")[:30]
    if title_prefix and state.db:
        timeline = await state.db.select("messages", {
            "content": f"like.*{title_prefix}*",
            "order": "created_at.asc",
            "limit": "50",
        })

    return JSONResponse({
        "task": task,
        "agent_logs": agent_logs,
        "feedback": feedback,
        "timeline": timeline,
    })


@app.post("/api/tasks/{task_id}/feedback")
async def api_task_feedback(task_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    rating = body.get("rating")
    comment = body.get("comment", "")
    needs_rework = body.get("needs_rework", False)

    if not rating:
        return JSONResponse({"ok": False, "error": "rating required"}, status_code=400)

    # Save feedback
    await state.save_feedback(
        task_id=task_id,
        agent="user",
        rating=_safe_int(rating, 3),
        comment=comment,
    )

    # Update scheduled_task review status
    if needs_rework:
        await state.update_scheduled_task(task_id, {
            "status": "in_progress",
            "review_status": "needs_rework",
        })
        await broadcast({"type": "tasks_update"})
    else:
        await state.update_scheduled_task(task_id, {
            "review_status": "approved",
        })

    return JSONResponse({"ok": True})


@app.post("/api/tasks/{task_id}/reopen")
async def api_task_reopen(task_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    context = body.get("context", "")

    await state.update_scheduled_task(task_id, {
        "status": "in_progress",
        "review_status": "needs_rework",
    })

    # Save context as a direct message to manager
    if context:
        await state.save_direct_message("manager", "direct_user", f"Правки к задаче #{task_id}: {context}")

    await broadcast({"type": "tasks_update"})
    return JSONResponse({"ok": True})


# ── REST: quests ──────────────────────────────────────────────────────────────

VALID_QUEST_TYPES = {"provide_token", "api_key", "approve", "top_up", "info"}


@app.post("/api/quests")
async def api_create_quest(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    title       = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    quest_type  = body.get("quest_type", "info")
    agent       = body.get("agent", "")
    xp_reward   = _safe_int(body.get("xp_reward", 10), 10)
    data        = body.get("data")

    if not title:
        return JSONResponse({"ok": False, "error": "empty title"}, status_code=400)
    if quest_type not in VALID_QUEST_TYPES:
        return JSONResponse({"ok": False, "error": f"invalid quest_type, use: {VALID_QUEST_TYPES}"}, status_code=400)

    quest = await state.create_quest(title, description, quest_type, agent, xp_reward, data)
    if not quest:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True, "quest": quest})


@app.get("/api/quests")
async def api_list_quests(status: str = "", limit: int = 50):
    quests = await state.get_quests(
        status=status or None,
        limit=min(limit, 200),
    )
    return JSONResponse({"quests": quests})


@app.post("/api/debug/create-quests/{task_id}")
async def api_debug_create_quests(task_id: int):
    """Debug: manually trigger quest creation for a scheduled task."""
    if not state.db:
        return JSONResponse({"error": "no db"}, status_code=500)
    tasks = await state.db.select("scheduled_tasks", {"id": f"eq.{task_id}", "limit": "1"})
    if not tasks:
        return JSONResponse({"error": "task not found"}, status_code=404)
    result = tasks[0].get("result", "")
    if not result:
        return JSONResponse({"error": "no result"}, status_code=400)
    await _notify_user_task_done(result)
    return JSONResponse({"ok": True, "message": "quests created"})


@app.put("/api/quests/{quest_id}/complete")
async def api_complete_quest(quest_id: int, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}

    response = body.get("response")

    # Before completing, fetch quest data to check if clarification needed
    quest_rows = await state.db.select("quests", {
        "id": f"eq.{quest_id}", "limit": "1",
        "select": "id,title,description,quest_type,data",
    }) if state.db else []
    quest_data = quest_rows[0] if quest_rows else {}

    ok = await state.complete_quest(quest_id, response)
    if not ok:
        return JSONResponse({"ok": False, "error": "db error or not found"}, status_code=500)

    # If user responded with a question/confusion → create clarification quest
    is_question = _is_clarification_needed(response)
    quest_action = quest_data.get("data", {}).get("action") if isinstance(quest_data.get("data"), dict) else None
    logger.info(f"[complete_quest] #{quest_id} is_question={is_question} action={quest_action} response={response!r:.80}")
    if is_question and quest_action == "user_action":
        asyncio.create_task(_create_clarification_quest(quest_data, response))
    else:
        # Check if this was a user_action quest — maybe trigger Phase 2
        asyncio.create_task(_check_phase2_trigger(quest_id, response))

    return JSONResponse({"ok": True})


# ── REST: briefing ───────────────────────────────────────────────────────────

@app.get("/api/briefing")
async def api_briefing():
    briefing = await state.get_briefing()
    return JSONResponse(briefing)


# ── REST: ideas board ─────────────────────────────────────────────────────────

@app.post("/api/ideas")
async def api_create_idea(request: Request):
    body = await request.json()
    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "empty content"}, status_code=400)
    idea = await state.create_idea(content)
    if not idea:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    ideas = await state.get_ideas()
    await broadcast({"type": "ideas_update", "ideas": ideas})
    asyncio.create_task(_plan_idea(idea["id"], content))
    return JSONResponse({"ok": True, "idea": idea})


@app.get("/api/ideas")
async def api_get_ideas():
    ideas = await state.get_ideas()
    return JSONResponse({"ideas": ideas})


@app.post("/api/ideas/{idea_id}/start")
async def api_start_idea(idea_id: int):
    idea = await state.start_idea(idea_id)
    if not idea:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    state._current_idea_id = idea_id
    ideas = await state.get_ideas()
    await broadcast({"type": "ideas_update", "ideas": ideas})
    await _forward_to_n8n(idea["content"])
    return JSONResponse({"ok": True})


async def _plan_idea(idea_id: int, content: str) -> None:
    """Call Anthropic Haiku to create an analysis + plan for an idea."""
    system = (
        "Ты — менеджер команды AI-агентов. Пользователь описывает идею. Твоя задача:\n"
        "1. Кратко описать суть идеи (2-3 предложения)\n"
        "2. Составить пошаговый план выполнения через агентов\n\n"
        "Доступные агенты: researcher (исследование, анализ конкурентов), writer (статьи, спецификации, ТЗ), "
        "coder (генерация кода), deployer (публикация в RSS)\n\n"
        "Формат ответа:\n"
        "**Анализ:** [краткое описание]\n\n"
        "**План:**\n"
        "1. Researcher: [что делает]\n"
        "2. Writer: [что делает]\n"
        "...\n\n"
        "Отвечай по-русски, кратко и конкретно."
    )
    api_key = ANTHROPIC_API_KEY
    if not api_key:
        await state.update_idea_plan(idea_id, "⚠️ ANTHROPIC_API_KEY не задан. Добавьте его в переменные окружения.")
        ideas = await state.get_ideas()
        await broadcast({"type": "ideas_update", "ideas": ideas})
        return
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 600,
                    "system": system,
                    "messages": [{"role": "user", "content": content}],
                },
            )
            data = r.json()
            plan_text = (data.get("content") or [{}])[0].get("text") or "Не удалось создать план."
    except Exception as e:
        plan_text = f"Ошибка при создании плана: {e}"
    await state.update_idea_plan(idea_id, plan_text)
    ideas = await state.get_ideas()
    await broadcast({"type": "ideas_update", "ideas": ideas})


# ── REST: articles + RSS feed for Яндекс Дзен ────────────────────────────────

BASE_URL = os.getenv("DASHBOARD_URL", "https://office.mopofipofue.beget.app")


@app.get("/api/deploy/artifacts")
async def api_list_artifacts(status: str = "", limit: int = 20):
    if not state.db:
        return JSONResponse({"artifacts": []})
    params = {
        "select": "id,task_id,project_name,status,deploy_result,created_at,updated_at",
        "order": "created_at.desc",
        "limit": str(min(limit, 50)),
    }
    if status:
        params["status"] = f"eq.{status}"
    artifacts = await state.db.select("code_artifacts", params)
    return JSONResponse({"artifacts": artifacts or []})


@app.get("/api/articles")
async def api_list_articles():
    articles = await state.get_articles()
    return JSONResponse({"articles": articles or []})


@app.post("/api/articles")
async def api_articles_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    title   = (body.get("title") or "").strip() or "Без названия"
    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "empty content"}, status_code=400)

    article = await state.save_article(title, content)
    if not article:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    article_url = f"{BASE_URL}/articles/{article['id']}"
    return JSONResponse({"ok": True, "id": article["id"], "article_url": article_url,
                         "rss_url": f"{BASE_URL}/rss"})


def _md_to_html(text: str) -> str:
    import re
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$',  r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$',   r'<h1>\1</h1>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', text)
    paragraphs = re.split(r'\n\n+', text)
    return ''.join(f'<p>{p.strip()}</p>' for p in paragraphs if p.strip())


@app.get("/articles/{article_id}")
async def get_article(article_id: int):
    a = await state.get_article_by_id(article_id)
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)
    title   = a["title"].replace("<", "&lt;")
    content = _md_to_html(a["content"])
    html = (f'<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
            f'<title>{title}</title><style>body{{font-family:Georgia,serif;'
            f'max-width:800px;margin:40px auto;padding:0 20px;line-height:1.7}}'
            f'h1,h2,h3{{font-family:sans-serif}}</style></head>'
            f'<body><h1>{title}</h1>{content}</body></html>')
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/rss")
async def rss_feed():
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    articles = await state.get_articles(limit=50)
    items = ""
    for a in articles:
        link = f"{BASE_URL}/articles/{a['id']}"
        items += f"""
    <item>
      <title>{esc(a['title'])}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <pubDate>{a['created_at']}</pubDate>
      <description><![CDATA[{_md_to_html(a['content'])}]]></description>
    </item>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Agent Office — Яндекс Дзен</title>
    <link>{BASE_URL}</link>
    <description>Автоматически генерируемые статьи</description>
    <language>ru</language>
    <atom:link href="{BASE_URL}/rss" rel="self" type="application/rss+xml"/>{items}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


# ── REST: agent memory ────────────────────────────────────────────────────────

@app.get("/api/memory")
async def api_get_memory(agent: str = "", memory_type: str = "", limit: int = 50):
    memories = await state.get_memory(
        agent=agent or None,
        memory_type=memory_type or None,
        limit=min(limit, 200),
    )
    return JSONResponse({"memories": memories})


@app.get("/api/memory/context/{agent}")
async def api_get_memory_context(agent: str, limit: int = 20):
    context = await state.get_memory_context(agent, limit=min(limit, 50))
    return JSONResponse({"context": context})


@app.post("/api/memory")
async def api_create_memory(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    agent = (body.get("agent") or "").strip()
    memory_type = (body.get("memory_type") or "").strip()
    content = (body.get("content") or "").strip()

    if not agent or not memory_type or not content:
        return JSONResponse({"ok": False, "error": "agent, memory_type, content required"}, status_code=400)

    mem = await state.save_memory(
        agent=agent,
        memory_type=memory_type,
        content=content,
        source_task_id=body.get("source_task_id"),
        importance=_safe_int(body.get("importance", 5), 5),
        tags=body.get("tags", []),
    )
    if not mem:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True, "memory": mem})


@app.delete("/api/memory/{memory_id}")
async def api_delete_memory(memory_id: int):
    ok = await state.delete_memory(memory_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True})


# ── REST: ecosystem map ──────────────────────────────────────────────────────

@app.get("/api/ecosystem")
async def api_get_ecosystem():
    """Get current ecosystem map (system memory)."""
    if not state.db:
        return JSONResponse({"ok": False, "error": "db not available"}, status_code=503)
    records = await state.db.select("agent_memory", {
        "agent": "eq.system",
        "memory_type": "eq.ecosystem_map",
        "order": "created_at.desc",
        "limit": "1",
    })
    if not records:
        return JSONResponse({"ok": True, "content": "", "updated_at": None})
    r = records[0]
    return JSONResponse({"ok": True, "content": r.get("content", ""), "updated_at": r.get("created_at"), "id": r.get("id")})


@app.put("/api/ecosystem")
async def api_update_ecosystem(req: Request):
    """Create or update the ecosystem map."""
    body = await req.json()
    content = body.get("content", "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "content required"}, status_code=400)
    if not state.db:
        return JSONResponse({"ok": False, "error": "db not available"}, status_code=503)
    # Delete existing and insert fresh
    existing = await state.db.select("agent_memory", {
        "agent": "eq.system",
        "memory_type": "eq.ecosystem_map",
    })
    for r in existing:
        await state.db.delete("agent_memory", {"id": r["id"]})
    mem = await state.db.insert_returning("agent_memory", {
        "agent": "system",
        "memory_type": "ecosystem_map",
        "content": content,
        "importance": 10,
        "tags": ["ecosystem", "infrastructure"],
    })
    return JSONResponse({"ok": True, "memory": mem[0] if mem else None})


# ── REST: user profile ───────────────────────────────────────────────────────

@app.get("/api/profile")
async def api_get_profile():
    profile = await state.get_profile()
    return JSONResponse({"profile": profile})


@app.put("/api/profile")
async def api_update_profile(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    category = (body.get("category") or "").strip()
    key = (body.get("key") or "").strip()
    value = (body.get("value") or "").strip()

    if not category or not key or not value:
        return JSONResponse({"ok": False, "error": "category, key, value required"}, status_code=400)

    result = await state.update_profile(
        category=category,
        key=key,
        value=value,
        source=body.get("source", "explicit"),
    )
    if not result:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True, "profile": result})


@app.delete("/api/profile/{profile_id}")
async def api_delete_profile(profile_id: int):
    ok = await state.delete_profile(profile_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True})


# ── REST: task feedback ──────────────────────────────────────────────────────

@app.get("/api/feedback")
async def api_get_feedback(agent: str = "", limit: int = 50):
    feedbacks = await state.get_feedback(
        agent=agent or None,
        limit=min(limit, 200),
    )
    return JSONResponse({"feedbacks": feedbacks})


@app.post("/api/feedback")
async def api_create_feedback(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    task_id = body.get("task_id")
    agent = (body.get("agent") or "").strip()
    rating = body.get("rating")

    if not task_id or not agent or not rating:
        return JSONResponse({"ok": False, "error": "task_id, agent, rating required"}, status_code=400)

    feedback = await state.save_feedback(
        task_id=_safe_int(task_id, 0),
        agent=agent,
        rating=_safe_int(rating, 3),
        comment=(body.get("comment") or "").strip(),
    )
    if not feedback:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True, "feedback": feedback})


# ── REST: agent errors + reflection ──────────────────────────────────────────

@app.get("/api/errors")
async def api_get_errors(agent: str = "", limit: int = 50):
    errors = await state.get_errors(
        agent=agent or None,
        limit=min(limit, 200),
    )
    return JSONResponse({"errors": errors})


@app.post("/api/errors")
async def api_create_error(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    agent = (body.get("agent") or "").strip()
    error_type = (body.get("error_type") or "").strip()
    error_detail = (body.get("error_detail") or "").strip()

    if not agent or not error_type or not error_detail:
        return JSONResponse({"ok": False, "error": "agent, error_type, error_detail required"}, status_code=400)

    err = await state.save_error(
        agent=agent,
        error_type=error_type,
        error_detail=error_detail,
        task_id=body.get("task_id"),
    )
    if not err:
        return JSONResponse({"ok": False, "error": "db error"}, status_code=500)
    return JSONResponse({"ok": True, "error_record": err})


@app.post("/api/errors/{error_id}/reflect")
async def api_reflect_error(error_id: int):
    """Use Haiku to analyze an error and generate reflection + lesson, save to memory."""
    errors = await state.get_errors(limit=200)
    err = next((e for e in errors if e["id"] == error_id), None)
    if not err:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    if err.get("reflection"):
        return JSONResponse({"ok": True, "already_reflected": True, "reflection": err["reflection"], "lesson": err.get("lesson")})

    api_key = ANTHROPIC_API_KEY
    if not api_key:
        return JSONResponse({"ok": False, "error": "ANTHROPIC_API_KEY not set"}, status_code=500)

    prompt = (
        f"Агент: {err['agent']}\n"
        f"Тип ошибки: {err['error_type']}\n"
        f"Детали: {err['error_detail']}\n\n"
        "Проанализируй ошибку. Ответь строго в формате:\n"
        "REFLECTION: [почему произошла ошибка, 1-2 предложения]\n"
        "LESSON: [что нужно делать иначе в будущем, 1 предложение]"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            text = (r.json().get("content") or [{}])[0].get("text", "")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"LLM call failed: {e}"}, status_code=500)

    reflection = ""
    lesson = ""
    for line in text.split("\n"):
        line = line.strip()
        if line.upper().startswith("REFLECTION:"):
            reflection = line.split(":", 1)[1].strip()
        elif line.upper().startswith("LESSON:"):
            lesson = line.split(":", 1)[1].strip()

    if not reflection:
        reflection = text[:200]
    if not lesson:
        lesson = "Нет конкретного урока."

    await state.update_error_reflection(error_id, reflection, lesson)

    # Save lesson to agent memory
    await state.save_memory(
        agent=err["agent"],
        memory_type="lesson",
        content=lesson,
        source_task_id=err.get("task_id"),
        importance=7,
        tags=["error_reflection", err["error_type"]],
    )

    return JSONResponse({"ok": True, "reflection": reflection, "lesson": lesson})


# ── REST: agent stats ────────────────────────────────────────────────────────

@app.get("/api/agents/stats")
async def api_agent_stats():
    stats = await state.get_agent_stats()
    return JSONResponse({"agents": stats})


# ── REST: direct chat with agent ─────────────────────────────────────────

ECOSYSTEM_CONTEXT = """
Инфраструктура команды:
- VPS: 85.198.64.70 — Docker контейнеры: clawdbot, deploy-service (порт 9090)
- Railway Backend: https://web-production-4e42e.up.railway.app (~/agent-office/)
- n8n: https://mopofipofue.beget.app — оркестрация задач
- Dashboard: http://85.198.64.70:3000 (~/projects/agent-dashboard/)
- Supabase: хранит задачи, память агентов, идеи, артефакты кода
"""

AGENT_CHAT_PROMPTS = {
    "manager": (
        "Ты — Виктор, старший менеджер проектов и оркестратор команды AI-агентов Agent Office. "
        "Прямой, прагматичный, умеешь чётко декомпозировать сложные задачи на конкретные подзадачи для команды. "
        "В команде координируешь Алекса (Researcher), Лену (Writer), Макса (Coder), Сашу (QA) и Deployer. "
        "В прямом чате помогаешь планировать проекты, создавать roadmap, расставлять приоритеты и стратегически мыслить."
    ),
    "researcher": (
        "Ты — Алекс, аналитик и исследователь команды Agent Office. "
        "Аналитический склад ума, любишь находить паттерны в данных, умеешь быстро изучать новые области. "
        "Специализируешься на поиске информации (используешь Tavily), анализе рынков, конкурентной разведке. "
        "В прямом чате помогаешь с исследованиями, анализом данных, поиском актуальной информации и трендов."
    ),
    "writer": (
        "Ты — Лена, контент-специалист и технический писатель команды Agent Office. "
        "Умеешь писать чётко и структурированно, переводить сложное в понятное. "
        "Пишешь статьи, ТЗ, спецификации, маркетинговые тексты. "
        "В прямом чате помогаешь с написанием текстов, формулировками, структурированием информации и редактурой."
    ),
    "coder": (
        "Ты — Макс, старший разработчик команды Agent Office. "
        "Прагматичный, пишешь чистый код, не усложняешь без нужды. "
        "Знаешь стек: FastAPI, Python, Next.js/React, TypeScript, Docker, PostgreSQL, Supabase. "
        "В прямом чате помогаешь с кодом, дебаггингом, архитектурными решениями и техническими вопросами."
    ),
    "qa": (
        "Ты — Саша, QA-инженер и перфекционист команды Agent Office. "
        "Находишь edge cases там, где другие не замечают, пишешь чёткие тест-сценарии. "
        "Проверяешь качество кода, логику, соответствие требованиям. "
        "В прямом чате помогаешь с анализом требований, поиском потенциальных проблем и стратегией тестирования."
    ),
    "deployer": (
        "Ты — Deployer, специалист по деплою команды Agent Office. "
        "Разворачиваешь приложения через Docker на VPS 85.198.64.70 используя deploy-service (порт 9090). "
        "Знаешь Railway, Docker Compose, nginx, CI/CD процессы. "
        "В прямом чате помогаешь с вопросами деплоя, инфраструктуры и DevOps."
    ),
}


@app.get("/api/chat/direct")
async def api_get_direct_chat(agent: str = "", limit: int = 30):
    if not agent:
        return JSONResponse({"ok": False, "error": "agent required"}, status_code=400)
    messages = await state.get_direct_messages(agent, limit=min(limit, 100))
    return JSONResponse({"messages": messages})


@app.post("/api/chat/direct")
async def api_chat_direct(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    agent = (body.get("agent") or "").strip()
    message = (body.get("message") or "").strip()

    if not agent or not message:
        return JSONResponse({"ok": False, "error": "agent and message required"}, status_code=400)
    if agent not in AGENT_DEFS:
        return JSONResponse({"ok": False, "error": f"unknown agent: {agent}"}, status_code=400)

    api_key = OPENAI_API_KEY or ANTHROPIC_API_KEY
    if not api_key:
        return JSONResponse({"ok": False, "error": "No LLM API key configured"}, status_code=500)

    use_openai = bool(OPENAI_API_KEY)

    # Load memory context and chat history
    memory_context = await state.get_memory_context(agent)
    history = await state.get_direct_messages(agent, limit=20)

    # Build system prompt
    base_prompt = AGENT_CHAT_PROMPTS.get(agent, f"Ты — {agent}, агент команды Agent Office.")
    memory_str = "\n".join(
        f"- [{m.get('memory_type', '')}] {m.get('content', '')}"
        for m in memory_context
    ) if memory_context else "Нет записей."

    system = (
        f"{base_prompt}\n"
        f"{ECOSYSTEM_CONTEXT}\n"
        f"Твоя память и уроки:\n{memory_str}\n\n"
        "Отвечай по-русски, кратко и по существу. Если не знаешь — скажи честно."
    )

    # Build messages for API
    api_messages = [{"role": m["role"], "content": m["content"]} for m in history]
    api_messages.append({"role": "user", "content": message})

    # Save user message
    await state.save_direct_message(agent, "direct_user", message)

    # Choose model based on agent type (researcher/qa → mini, others → full)
    openai_model = "gpt-4o-mini" if agent in ("researcher", "qa") else "gpt-4o"

    # Call LLM API (OpenAI preferred, Anthropic fallback)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            if use_openai:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": openai_model,
                        "max_tokens": 1024,
                        "messages": [{"role": "system", "content": system}] + api_messages,
                    },
                )
                if r.status_code != 200:
                    return JSONResponse({"ok": False, "error": f"OpenAI {r.status_code}: {r.text[:300]}"}, status_code=502)
                data = r.json()
                reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            else:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 1024,
                        "system": system,
                        "messages": api_messages,
                    },
                )
                if r.status_code != 200:
                    return JSONResponse({"ok": False, "error": f"Anthropic {r.status_code}: {r.text[:300]}"}, status_code=502)
                data = r.json()
                content_blocks = data.get("content")
                reply = content_blocks[0].get("text", "") if content_blocks else ""
            if not reply:
                return JSONResponse({"ok": False, "error": f"Empty LLM response"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"LLM error: {e}"}, status_code=500)

    # Save agent response
    await state.save_direct_message(agent, "direct_agent", reply)

    return JSONResponse({"ok": True, "reply": reply, "agent": agent})


# ── REST: analytics ──────────────────────────────────────────────────────

@app.get("/api/analytics/overview")
async def api_analytics_overview(days: int = 7):
    overview = await state.get_analytics_overview(days=min(days, 90))
    return JSONResponse(overview)


# ── Helper: auto-reflect on errors ───────────────────────────────────────────

async def _auto_reflect_error(error_id: int):
    """Background task: call Haiku to reflect on a new error."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"http://localhost:{os.getenv('PORT', '8080')}/api/errors/{error_id}/reflect"
            )
    except Exception as e:
        print(f"[auto_reflect] error: {e}")


# ── Helper: link completed task result to scheduled_task ─────────────────────

async def _link_result_to_scheduled_task(result: str):
    """When a task completes, find the linked scheduled_task and attach the result."""
    if not state.db:
        return
    try:
        pipeline_task_id = state._current_task_id
        scheduled = None

        # Strategy 1: find by linked_task_id (reliable — set by /run endpoint)
        if pipeline_task_id:
            linked = await state.db.select("scheduled_tasks", {
                "linked_task_id": f"eq.{pipeline_task_id}",
                "limit": "1",
            })
            if linked:
                scheduled = linked[0]

        # Strategy 2: fallback — find most recent in_progress (for Command Center tasks)
        if not scheduled:
            fallback = await state.db.select("scheduled_tasks", {
                "status": "eq.in_progress",
                "order": "created_at.desc",
                "limit": "1",
            })
            if fallback:
                scheduled = fallback[0]

        if not scheduled:
            return

        task_id = scheduled["id"]

        # Update with result and set pending_review
        update_data = {
            "status": "done",
            "result": result,
            "review_status": "pending_review",
        }
        if pipeline_task_id and not scheduled.get("linked_task_id"):
            update_data["linked_task_id"] = pipeline_task_id

        await state.update_scheduled_task(task_id, update_data)

        # Broadcast detailed event
        await broadcast({
            "type": "task_completed",
            "task_id": task_id,
            "title": scheduled.get("title", ""),
            "result_preview": result[:200] if result else "",
            "assigned_agent": scheduled.get("assigned_agent", "manager"),
        })
        await broadcast({"type": "tasks_update"})
    except Exception as e:
        print(f"[link_result] error: {e}")


def _is_clarification_needed(response: str | None) -> bool:
    """Check if user's response indicates confusion rather than actual data."""
    if not response:
        return False
    r = response.lower().strip()
    indicators = [
        "не понимаю", "не понял", "что нужно", "что это", "что значит",
        "как это", "где взять", "где найти", "что предоставить", "что дать",
        "можно конкретн", "поясни", "объясни", "уточни", "какой формат",
        "что именно", "не знаю что", "хз", "непонятно", "?",
    ]
    return any(ind in r for ind in indicators)


async def _create_clarification_quest(original_quest: dict, user_question: str):
    """Re-create quest with detailed explanation based on user's question."""
    try:
        if not state.db:
            return
        original_title = original_quest.get("title", "")
        original_data = original_quest.get("data", {}) or {}
        task_id = original_data.get("task_id")

        # Build a more detailed description explaining what exactly is needed
        detail = (
            f"Ты спросил: \"{user_question}\"\n\n"
            f"Что нужно: {original_title}\n\n"
            "Если у тебя нет этого — напиши 'пропустить' или 'нет доступа'. "
            "Если непонятно что именно — опиши свою ситуацию, и мы адаптируем план."
        )

        new_quest = await state.create_quest(
            title=f"Уточнение: {original_title[:60]}",
            description=detail,
            quest_type="info",
            agent="manager",
            xp_reward=5,
            data={
                **original_data,
                "action": "user_action",
                "is_clarification": True,
                "original_question": user_question,
                "input_required": True,
                "input_label": "Введи данные, или напиши 'пропустить' / задай вопрос",
                "input_placeholder": "Данные, или опиши что непонятно...",
            },
        )
        if new_quest:
            await broadcast({"type": "quest_created", "quest": new_quest})
            logger.info(f"[clarification] Created quest #{new_quest['id']} for '{original_title[:40]}'")
    except Exception as e:
        logger.error(f"[clarification] error: {e}", exc_info=True)


async def _check_phase2_trigger(quest_id: int, response: str | None):
    """When all user_action quests for a task are completed — trigger Phase 2 build."""
    try:
        if not state.db:
            return

        # Get the completed quest to find task_id
        quests = await state.db.select("quests", {"id": f"eq.{quest_id}", "limit": "1"})
        if not quests:
            return
        quest = quests[0]
        data = quest.get("data") or {}
        if data.get("action") != "user_action":
            return  # Not a user_action quest, skip

        task_id = data.get("task_id")
        if not task_id:
            return

        # Find ALL user_action quests for this task
        all_quests = await state.db.select("quests", {
            "data->>action": "eq.user_action",
            "data->>task_id": f"eq.{task_id}",
        })
        if not all_quests:
            return

        # Check if all are completed
        pending = [q for q in all_quests if q.get("status") != "completed"]
        if pending:
            return  # Still have uncompleted quests

        # All done! Collect responses
        collected_inputs = {}
        for q in all_quests:
            q_data = q.get("data") or {}
            label = q_data.get("input_label", q.get("title", ""))
            resp = q.get("response")
            if isinstance(resp, dict):
                resp = resp.get("response", "")
            collected_inputs[label] = resp or "подтверждено"

        # Get original task info
        scheduled = await state.db.select("scheduled_tasks", {
            "id": f"eq.{task_id}",
            "limit": "1",
        })
        original_task = scheduled[0]["title"] if scheduled else "Задача"

        # Build Phase 2 task description
        inputs_text = "\n".join(f"- {k}: {v}" for k, v in collected_inputs.items())
        phase2_task = (
            f"ФАЗА 2 — РЕАЛИЗАЦИЯ. Пользователь предоставил все данные.\n\n"
            f"Оригинальная задача: {original_task}\n\n"
            f"Данные от пользователя:\n{inputs_text}\n\n"
            f"Теперь СДЕЛАЙ реальный продукт. Не план — ГОТОВЫЙ результат. "
            f"Код, тексты, конфиги — всё что нужно для запуска."
        )

        # Notify user
        await broadcast({
            "type": "agent_update",
            "agent": "manager",
            "status": "thinking",
            "message": f"Все данные получены! Запускаю реализацию...",
        })

        # Trigger n8n Manager with Phase 2 task
        if N8N_MANAGER_WEBHOOK:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    N8N_MANAGER_WEBHOOK,
                    json={"task": phase2_task},
                )
                print(f"[phase2] Triggered n8n: {resp.status_code}")
        else:
            print("[phase2] No N8N_MANAGER_WEBHOOK configured")

    except Exception as e:
        print(f"[phase2] error: {e}")


async def _notify_user_task_done(result: str):
    """Create quests from task result — review quest + individual action items."""
    try:
        if not state.db:
            logger.warning("[notify_user] no state.db, skipping")
            return
        logger.info("[notify_user] Querying scheduled_tasks...")
        scheduled = await state.db.select("scheduled_tasks", {
            "status": "eq.done",
            "review_status": "eq.pending_review",
            "order": "created_at.desc",
            "limit": "1",
        })
        logger.info(f"[notify_user] Found {len(scheduled)} scheduled tasks")
        task_id = scheduled[0]["id"] if scheduled else None

        # Parse structured result for clean title and user_actions
        clean_title = "Задача"
        user_actions = []
        summary = ""
        try:
            parsed = json.loads(result)
            if parsed.get("version") == 2:
                clean_title = parsed.get("title", "") or "Задача"
                user_actions = parsed.get("user_actions", [])
                summary = parsed.get("summary", "")
        except (json.JSONDecodeError, TypeError):
            if scheduled:
                clean_title = scheduled[0].get("title", "Задача")

        logger.info(f"[notify_user] title={clean_title}, user_actions={len(user_actions)}, task_id={task_id}")

        # Create main review quest with clean title
        quest = await state.create_quest(
            title=f"Проверь: {clean_title[:60]}",
            description=summary or "Агенты выполнили задачу. Проверь результат и оцени.",
            quest_type="approve",
            agent="manager",
            xp_reward=15,
            data={"task_id": task_id, "action": "review"},
        )
        if quest:
            await broadcast({"type": "quest_created", "quest": quest})
            asyncio.create_task(tg_bot.notify_quest(quest))

        # Create individual quests for each user action — max 3, only if input is truly needed
        input_actions = [a for a in user_actions if isinstance(a, dict) and a.get("input_required")]
        if not input_actions:
            # Only create action quests for string actions that look like they need user input
            input_actions = [a for a in user_actions if isinstance(a, str) and any(
                kw in a.lower() for kw in ["укажи", "введи", "добавь", "настрой", "предоставь", "provide", "enter"]
            )]
        for i, action in enumerate(input_actions[:3]):  # max 3 action quests
            action_text = action if isinstance(action, str) else action.get("text", "")
            if not action_text:
                continue
            action_quest = await state.create_quest(
                title=action_text[:80],
                description=f"Для задачи: {clean_title[:60]}",
                quest_type="info",
                agent="manager",
                xp_reward=10,
                data={
                    "task_id": task_id,
                    "action": "user_action",
                    "action_index": i,
                    "total_actions": len(user_actions),
                    "input_required": True,
                    "input_label": action_text[:80],
                    "input_placeholder": "Укажи данные или напиши что готово...",
                },
            )
            if action_quest:
                await broadcast({"type": "quest_created", "quest": action_quest})
                asyncio.create_task(tg_bot.notify_quest(action_quest))

        # Also try to update related idea
        if scheduled:
            await _link_result_to_idea(scheduled[0]["title"], result)

    except Exception as e:
        logger.error(f"[notify_user] error: {e}", exc_info=True)


async def _link_result_to_idea(task_title: str, result: str):
    """Try to find and update the related idea with the result."""
    if not state.db:
        return
    try:
        ideas = await state.db.select("ideas", {
            "status": "in.('active','planned','planning','done')",
            "order": "created_at.desc",
            "limit": "5",
        })
        for idea in ideas:
            # Fuzzy match: if idea content appears in task title or vice versa
            ic = idea.get("content", "").lower()[:50]
            tt = task_title.lower()[:50]
            if ic[:20] in tt or tt[:20] in ic:
                await state.db.update("ideas", {"id": f"eq.{idea['id']}"}, {
                    "result": result,
                    "status": "done",
                })
                return
    except Exception as e:
        print(f"[link_idea] error: {e}")


# ── Background: stuck task timeout checker ────────────────────────────────────

async def _task_timeout_checker():
    """Mark stuck tasks as error every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        if not state.db:
            continue
        try:
            cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
            stuck = await state.db.select("tasks", {
                "status": "eq.processing",
                "created_at": f"lt.{cutoff}",
            })
            for task in (stuck or []):
                await state.db.update("tasks", {"id": task["id"]}, {
                    "status": "error",
                    "summary": "Timeout: задача не получила ответ от n8n в течение 10 минут",
                })
                print(f"[timeout] Task {task['id']} marked as error (stuck >10min)")
            if stuck:
                await broadcast({"type": "tasks_update"})
        except Exception as e:
            print(f"[timeout_checker] error: {e}")


# ── Helper: forward task to n8n ───────────────────────────────────────────────

async def _forward_to_n8n(task: str):
    if not N8N_MANAGER_WEBHOOK:
        await broadcast({
            "type": "chat",
            "message": {
                "role": "manager", "name": "Manager", "emoji": "🎯", "color": "#a78bfa",
                "content": "⚠️ N8N_MANAGER_WEBHOOK не настроен.",
                "time": "00:00",
            },
        })
        return

    task_id = await state.save_task(task)
    state._current_task_id = task_id
    await broadcast({"type": "tasks_update"})
    asyncio.create_task(_call_n8n(task, task_id))


async def _call_n8n(task: str, task_id: int):
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(N8N_MANAGER_WEBHOOK, json={
                "task": task,
                "taskId": task_id,
                "callbackUrl": f"{BASE_URL}/api/n8n/callback",
            })
            if resp.status_code >= 400:
                raise Exception(f"n8n returned {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[_call_n8n] ERROR for task {task_id}: {e}")
        if state.db:
            try:
                await state.db.update("tasks", {"id": task_id}, {
                    "status": "error",
                    "summary": f"n8n error: {str(e)[:500]}",
                })
            except Exception:
                pass
        await broadcast({
            "type": "chat",
            "message": {
                "role": "system", "name": "System", "emoji": "\u26a0\ufe0f", "color": "#ff2a6d",
                "content": f"\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438 \u0437\u0430\u0434\u0430\u0447\u0438 \u0432 n8n: {str(e)[:200]}",
                "time": datetime.now().strftime("%H:%M"),
            },
        })
        await broadcast({"type": "tasks_update"})


# ── TG notifications on key events ───────────────────────────────────────────

async def _maybe_notify_tg(payload: dict):
    """Send Telegram notification on significant status changes."""
    agent  = payload.get("agent", "")
    status = payload.get("status", "")
    msg    = payload.get("message", "")
    task   = payload.get("task", "")

    # Notify when manager goes idle (= task complete)
    if agent == "manager" and status == "idle":
        summary = msg or "Команда завершила работу."
        short   = summary[:300] + ("…" if len(summary) > 300 else "")
        asyncio.create_task(tg_bot.notify(f"✅ <b>Задача выполнена</b>\n\n{short}"))
        return

    # Notify when manager shares a plan
    if agent == "manager" and status == "thinking" and msg:
        short = msg[:200] + ("…" if len(msg) > 200 else "")
        asyncio.create_task(tg_bot.notify(f"🎯 <b>Manager</b>: {short}"))
        return

    # Notify when worker starts working
    if agent != "manager" and status == "working" and task:
        asyncio.create_task(tg_bot.notify_agent_progress(agent, task, 0))


# ── Admin endpoints ──────────────────────────────────────────────────────────

@app.get("/api/admin/health")
async def api_admin_health():
    """System health overview."""
    return JSONResponse({
        "version": APP_VERSION,
        "uptime_sec": (datetime.utcnow() - _startup_time).total_seconds(),
        "ws_clients": len(clients),
        "tg_bot_active": _tg_app is not None,
        "n8n_webhook": N8N_MANAGER_WEBHOOK,
        "db_connected": state.db is not None,
        "agents": state.agent_states(),
    })


@app.post("/api/admin/agents/reset")
async def api_admin_reset_agents():
    """Reset all agents to idle status."""
    for key in state.agents:
        state.agents[key].status = "idle"
        state.agents[key].task = ""
        state.agents[key].progress = 0
    await broadcast({
        "type": "init",
        "agents": state.agent_states(),
        "history": state.history[-80:],
    })
    return JSONResponse({"ok": True})


@app.post("/api/admin/tasks/{task_id}/retry")
async def api_admin_retry_task(task_id: int):
    """Re-run a failed or completed task."""
    if not state.db:
        return JSONResponse({"error": "no db"}, status_code=500)
    rows = await state.db.select("scheduled_tasks", {"id": f"eq.{task_id}", "limit": "1"})
    if not rows:
        return JSONResponse({"error": "not found"}, status_code=404)
    task = rows[0]
    # Reset status
    await state.db.update("scheduled_tasks", task_id, {
        "status": "in_progress", "review_status": "none", "result": None,
    })
    # Forward to n8n
    await _forward_to_n8n(task["title"])
    return JSONResponse({"ok": True})


@app.post("/api/admin/errors/reflect-all")
async def api_admin_reflect_all_errors():
    """Trigger AI reflection on all unreflected errors (max 10)."""
    if not state.db:
        return JSONResponse({"error": "no db"}, status_code=500)
    errors = await state.get_errors(limit=50)
    unreflected = [e for e in (errors or []) if not e.get("reflection")]
    count = 0
    for err in unreflected[:10]:
        asyncio.create_task(_auto_reflect_error(err["id"]))
        count += 1
    return JSONResponse({"ok": True, "count": count})


@app.get("/api/admin/metrics")
async def api_admin_metrics():
    """System metrics for monitoring."""
    metrics = {
        "version": APP_VERSION,
        "uptime_sec": (datetime.utcnow() - _startup_time).total_seconds(),
        "ws_clients": len(clients),
        "agents_active": sum(
            1 for a in state.agents.values()
            if a.status in ("working", "thinking")
        ),
    }
    if state.db:
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        errors = await state.db.select("agent_errors", {
            "created_at": f"gt.{cutoff}",
        })
        metrics["errors_24h"] = len(errors) if errors else 0
    return JSONResponse(metrics)
