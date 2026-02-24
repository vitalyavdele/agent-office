"""
State manager for n8n-based agent orchestration.
Persists messages and tasks to Supabase (if configured).
Falls back to in-memory only when Supabase is not configured.
"""
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable, Optional

import httpx


# ── Agent catalogue ───────────────────────────────────────────────────────────
AGENT_DEFS = {
    "manager":    {"id": 0, "name": "Manager",    "role": "Orchestrator",   "emoji": "🎯", "color": "#a78bfa"},
    "researcher": {"id": 1, "name": "Researcher", "role": "Web Researcher", "emoji": "🔍", "color": "#38bdf8"},
    "writer":     {"id": 2, "name": "Writer",     "role": "Content Writer", "emoji": "✍️", "color": "#4ade80"},
    "coder":      {"id": 3, "name": "Coder",      "role": "Code Engineer",  "emoji": "💻", "color": "#facc15"},
    "analyst":    {"id": 4, "name": "Analyst",    "role": "Data Analyst",   "emoji": "📊", "color": "#fb7185"},
    "ux-auditor": {"id": 5, "name": "UX Auditor", "role": "Design Analyst", "emoji": "🎨", "color": "#f97316"},
    "site-coder": {"id": 6, "name": "Site Coder", "role": "Frontend Dev",   "emoji": "🌐", "color": "#22d3ee"},
    "deployer":   {"id": 7, "name": "Deployer",   "role": "Git Publisher",  "emoji": "🚀", "color": "#a3e635"},
}


# ── State ─────────────────────────────────────────────────────────────────────
@dataclass
class AgentState:
    key:      str
    id:       int
    name:     str
    role:     str
    emoji:    str
    color:    str
    status:   str = "idle"
    task:     str = "Свободен"
    progress: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Supabase REST helper ──────────────────────────────────────────────────────
class SupabaseClient:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    async def insert(self, table: str, data: dict) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                json=data,
            )

    async def insert_returning(self, table: str, data: dict) -> list:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.url}/rest/v1/{table}",
                headers={**self.headers, "Prefer": "return=representation"},
                json=data,
            )
            return r.json() if r.status_code in (200, 201) else []

    async def select(self, table: str, params: dict) -> list:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{self.url}/rest/v1/{table}",
                headers={**self.headers, "Prefer": ""},
                params=params,
            )
            return r.json() if r.status_code == 200 else []

    async def update(self, table: str, match: dict, data: dict) -> None:
        params = {k: f"eq.{v}" for k, v in match.items()}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.patch(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                params=params,
                json=data,
            )


# ── State manager ─────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self, supabase_url: Optional[str] = None, supabase_key: Optional[str] = None):
        self.agents: dict[str, AgentState] = {
            k: AgentState(key=k, **v) for k, v in AGENT_DEFS.items()
        }
        self.history: list[dict] = []
        self._current_task_id: Optional[int] = None

        self.db: Optional[SupabaseClient] = None
        if supabase_url and supabase_key:
            self.db = SupabaseClient(supabase_url, supabase_key)
            print("[Supabase] client configured")
        else:
            print("[Supabase] not configured — in-memory only")

    # ── Load from DB on startup ───────────────────────────────────────────────

    async def load_history(self) -> None:
        """Load last 100 messages from Supabase into memory."""
        if not self.db:
            return
        try:
            rows = await self.db.select("messages", {
                "select": "role,name,emoji,color,content,msg_time",
                "order": "created_at.asc",
                "limit": "100",
            })
            self.history = [
                {
                    "role":    r["role"],
                    "name":    r.get("name") or "",
                    "emoji":   r.get("emoji") or "",
                    "color":   r.get("color") or "",
                    "content": r["content"],
                    "time":    r.get("msg_time") or "",
                }
                for r in rows
                if isinstance(r, dict)
            ]
            print(f"[Supabase] loaded {len(self.history)} messages from DB")
        except Exception as e:
            print(f"[Supabase] load_history error: {e}")

    # ── Save message fire-and-forget ──────────────────────────────────────────

    def _save_message(self, msg: dict) -> None:
        if not self.db:
            return
        asyncio.create_task(self._do_save_message(msg))

    async def _do_save_message(self, msg: dict) -> None:
        try:
            await self.db.insert("messages", {
                "role":     msg["role"],
                "name":     msg.get("name", ""),
                "emoji":    msg.get("emoji", ""),
                "color":    msg.get("color", ""),
                "content":  msg["content"],
                "msg_time": msg.get("time", ""),
            })
        except Exception as e:
            print(f"[Supabase] save_message error: {e}")

    # ── Task tracking ─────────────────────────────────────────────────────────

    async def save_task(self, content: str) -> Optional[int]:
        """Save a new task to DB and return its id."""
        if not self.db:
            return None
        try:
            rows = await self.db.insert_returning("tasks", {
                "content": content,
                "status": "processing",
            })
            if rows:
                return rows[0].get("id")
        except Exception as e:
            print(f"[Supabase] save_task error: {e}")
        return None

    async def finish_task(self, task_id: int, summary: str = "") -> None:
        if not self.db or not task_id:
            return
        try:
            await self.db.update("tasks", {"id": task_id}, {
                "status": "done",
                "summary": summary[:500] if summary else "",
                "finished_at": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[Supabase] finish_task error: {e}")

    async def _finish_latest_processing(self, summary: str = "") -> None:
        """Fallback: mark the most recent 'processing' task done when task_id was lost."""
        try:
            rows = await self.db.select("tasks", {
                "select": "id",
                "status": "eq.processing",
                "order": "created_at.desc",
                "limit": "1",
            })
            if rows and isinstance(rows, list) and rows[0].get("id"):
                await self.finish_task(rows[0]["id"], summary)
        except Exception as e:
            print(f"[Supabase] _finish_latest_processing error: {e}")

    async def get_tasks(self, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            return await self.db.select("tasks", {
                "select": "id,created_at,content,status,summary,finished_at",
                "order": "created_at.desc",
                "limit": str(limit),
            })
        except Exception as e:
            print(f"[Supabase] get_tasks error: {e}")
            return []

    # ── Public API ────────────────────────────────────────────────────────────

    def agent_states(self) -> list[dict]:
        return [a.to_dict() for a in self.agents.values()]

    async def apply_callback(self, broadcast: Callable, payload: dict):
        """
        Process a callback from n8n and broadcast updates to all WS clients.

        Expected payload fields:
          agent    — agent key (manager|researcher|writer|coder|analyst)
          status   — idle|thinking|working|done
          task     — current task description (optional)
          progress — 0-100 (optional)
          message  — chat message to display (optional)
        """
        key = payload.get("agent", "")
        if key not in self.agents:
            return

        agent = self.agents[key]

        if "status" in payload:
            agent.status = payload["status"]
        if "task" in payload:
            agent.task = payload["task"][:120]
        if "progress" in payload:
            agent.progress = int(payload["progress"])

        await broadcast({"type": "agents", "agents": self.agent_states()})

        if payload.get("message", "").strip():
            msg = {
                "role":    key,
                "name":    agent.name,
                "emoji":   agent.emoji,
                "color":   agent.color,
                "content": payload["message"].strip(),
                "time":    datetime.now().strftime("%H:%M"),
            }
            self.history.append(msg)
            if len(self.history) > 200:
                self.history.pop(0)
            self._save_message(msg)
            await broadcast({"type": "chat", "message": msg})

        # When manager goes idle, mark current task as done
        if key == "manager" and payload.get("status") == "idle":
            task_id = self._current_task_id
            self._current_task_id = None
            summary = payload.get("message", "")
            if task_id:
                asyncio.create_task(self.finish_task(task_id, summary))
            elif self.db:
                # Fallback: after server restart _current_task_id is lost —
                # mark the most recent processing task as done anyway
                asyncio.create_task(self._finish_latest_processing(summary))

    def add_user_message(self, content: str) -> dict:
        msg = {
            "role":    "user",
            "name":    "Вы",
            "emoji":   "👤",
            "color":   "#6366f1",
            "content": content,
            "time":    datetime.now().strftime("%H:%M"),
        }
        self.history.append(msg)
        if len(self.history) > 200:
            self.history.pop(0)
        self._save_message(msg)
        return msg
