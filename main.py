import asyncio
import os

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from agents import StateManager

app = FastAPI()
templates = Jinja2Templates(directory="templates")

state = StateManager(
    supabase_url=os.getenv("SUPABASE_URL", ""),
    supabase_key=os.getenv("SUPABASE_ANON_KEY", ""),
)

# URL of the n8n Manager webhook (set via environment variable)
N8N_MANAGER_WEBHOOK = os.getenv("N8N_MANAGER_WEBHOOK", "")

clients: set[WebSocket] = set()


@app.on_event("startup")
async def startup():
    await state.load_history()


async def broadcast(event: dict):
    dead = set()
    for ws in list(clients):
        try:
            await ws.send_json(event)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── WebSocket — browser ↔ dashboard ──────────────────────────────────────────

@app.websocket("/ws")
async def ws_handler(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)

    # Send current state to newly connected client
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


# ── REST: receive task from browser (alternative to WS) ──────────────────────

@app.post("/api/task")
async def api_task(request: Request):
    body = await request.json()
    content = body.get("content", "").strip()
    if not content:
        return JSONResponse({"ok": False, "error": "empty content"}, status_code=400)

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
    return JSONResponse({"ok": True})


# ── REST: task history ────────────────────────────────────────────────────────

@app.get("/api/tasks")
async def api_tasks():
    tasks = await state.get_tasks(limit=50)
    return JSONResponse({"tasks": tasks})


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

    # Save task to Supabase and track id for completion
    task_id = await state.save_task(task)
    state._current_task_id = task_id

    # Notify clients about updated task list
    await broadcast({"type": "tasks_update"})

    # Fire-and-forget: don't block on n8n response.
    # Results come back asynchronously via /api/n8n/callback.
    asyncio.create_task(_call_n8n(task))


async def _call_n8n(task: str):
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            await client.post(N8N_MANAGER_WEBHOOK, json={"task": task})
    except Exception:
        pass  # Agents send results via callback — errors here are non-fatal
