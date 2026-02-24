import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates

from agents import StateManager
import tg_bot

# ── State ─────────────────────────────────────────────────────────────────────

state = StateManager(
    supabase_url=os.getenv("SUPABASE_URL", ""),
    supabase_key=os.getenv("SUPABASE_ANON_KEY", ""),
)

N8N_MANAGER_WEBHOOK = os.getenv("N8N_MANAGER_WEBHOOK", "")
clients: set[WebSocket] = set()

# ── Lifespan: start/stop TG bot alongside FastAPI ────────────────────────────

_tg_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _tg_app
    await state.load_history()

    _tg_app = tg_bot.create_app()
    if _tg_app:
        tg_bot.set_forward(_forward_to_n8n)
        await _tg_app.initialize()
        await _tg_app.start()
        await _tg_app.updater.start_polling(drop_pending_updates=True)
        tg_bot.set_bot(_tg_app.bot)

    yield  # ── server running ──

    if _tg_app:
        await _tg_app.updater.stop()
        await _tg_app.stop()
        await _tg_app.shutdown()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


# ── Broadcast to all WS clients ───────────────────────────────────────────────

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
    return JSONResponse({"ok": True})


# ── REST: task history ────────────────────────────────────────────────────────

@app.get("/api/tasks")
async def api_tasks():
    tasks = await state.get_tasks(limit=50)
    return JSONResponse({"tasks": tasks})


# ── REST: articles + RSS feed for Яндекс Дзен ────────────────────────────────

RAILWAY_URL = "https://web-production-4e42e.up.railway.app"


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

    article     = state.save_article(title, content)
    article_url = f"{RAILWAY_URL}/articles/{article['id']}"
    return JSONResponse({"ok": True, "id": article["id"], "article_url": article_url,
                         "rss_url": f"{RAILWAY_URL}/rss"})


@app.get("/articles/{article_id}")
async def get_article(article_id: int):
    for a in state.articles:
        if a["id"] == article_id:
            import re
            def md_to_html(text: str) -> str:
                text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
                text = re.sub(r'^## (.+)$',  r'<h2>\1</h2>', text, flags=re.MULTILINE)
                text = re.sub(r'^# (.+)$',   r'<h1>\1</h1>', text, flags=re.MULTILINE)
                text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
                text = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', text)
                paragraphs = re.split(r'\n\n+', text)
                return ''.join(f'<p>{p.strip()}</p>' for p in paragraphs if p.strip())
            title   = a["title"].replace("<", "&lt;")
            content = md_to_html(a["content"])
            html = (f'<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">'
                    f'<title>{title}</title><style>body{{font-family:Georgia,serif;'
                    f'max-width:800px;margin:40px auto;padding:0 20px;line-height:1.7}}'
                    f'h1,h2,h3{{font-family:sans-serif}}</style></head>'
                    f'<body><h1>{title}</h1>{content}</body></html>')
            return Response(content=html, media_type="text/html; charset=utf-8")
    return JSONResponse({"error": "not found"}, status_code=404)


@app.get("/rss")
async def rss_feed():
    import re

    def md_to_html(text: str) -> str:
        text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
        text = re.sub(r'^## (.+)$',  r'<h2>\1</h2>', text, flags=re.MULTILINE)
        text = re.sub(r'^# (.+)$',   r'<h1>\1</h1>', text, flags=re.MULTILINE)
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'\*(.+?)\*',     r'<em>\1</em>', text)
        paragraphs = re.split(r'\n\n+', text)
        return ''.join(f'<p>{p.strip()}</p>' for p in paragraphs if p.strip())

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    articles = state.get_articles(limit=50)
    items = ""
    for a in articles:
        link = f"{RAILWAY_URL}/articles/{a['id']}"
        items += f"""
    <item>
      <title>{esc(a['title'])}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <pubDate>{a['created_at']}</pubDate>
      <description><![CDATA[{md_to_html(a['content'])}]]></description>
    </item>"""

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Agent Office — Яндекс Дзен</title>
    <link>{RAILWAY_URL}</link>
    <description>Автоматически генерируемые статьи</description>
    <language>ru</language>
    <atom:link href="{RAILWAY_URL}/rss" rel="self" type="application/rss+xml"/>{items}
  </channel>
</rss>"""
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")


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
    asyncio.create_task(_call_n8n(task))


async def _call_n8n(task: str):
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            await client.post(N8N_MANAGER_WEBHOOK, json={"task": task})
    except Exception:
        pass


# ── TG notifications on key events ───────────────────────────────────────────

async def _maybe_notify_tg(payload: dict):
    """Send Telegram notification on significant status changes."""
    agent  = payload.get("agent", "")
    status = payload.get("status", "")
    msg    = payload.get("message", "")

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
