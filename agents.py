"""
State manager for n8n-based agent orchestration.
Persists messages and tasks to Supabase (if configured).
Falls back to in-memory only when Supabase is not configured.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

import httpx


# ── Agent catalogue ───────────────────────────────────────────────────────────
AGENT_DEFS = {
    "manager":    {"id": 0, "name": "Manager",    "role": "Orchestrator",      "emoji": "🎯", "color": "#a78bfa"},
    "researcher": {"id": 1, "name": "Researcher", "role": "Web Researcher",    "emoji": "🔍", "color": "#38bdf8"},
    "writer":     {"id": 2, "name": "Writer",     "role": "Spec & Content",    "emoji": "✍️", "color": "#34d399"},
    "coder":      {"id": 3, "name": "Coder",      "role": "Code Generator",    "emoji": "💻", "color": "#f472b6"},
    "deployer":   {"id": 4, "name": "Deployer",   "role": "Publisher",         "emoji": "🚀", "color": "#fb923c"},
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
        return {
            "name": self.key,
            "emoji": self.emoji,
            "color": self.color,
            "status": self.status,
            "task": self.task if self.task != "Свободен" else None,
            "progress": self.progress,
        }


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
        self.articles: list[dict] = []
        self._article_counter: int = 0
        self._current_task_id: Optional[int] = None

        self.ideas: list[dict] = []
        self._idea_counter: int = 0
        self._current_idea_id: Optional[int] = None

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

        await broadcast({"type": "agent_update", "agent": agent.to_dict()})

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

        # When manager goes idle, mark current task and idea as done
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
            idea_id = self._current_idea_id
            self._current_idea_id = None
            if idea_id:
                self.finish_idea(idea_id, summary)

    # ── Ideas board ───────────────────────────────────────────────────────────

    def create_idea(self, content: str) -> dict:
        self._idea_counter += 1
        idea = {
            "id":         self._idea_counter,
            "content":    content,
            "status":     "planning",
            "plan_text":  None,
            "result":     None,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        self.ideas.insert(0, idea)
        if len(self.ideas) > 100:
            self.ideas.pop()
        return idea

    def update_idea_plan(self, idea_id: int, plan_text: str) -> None:
        for idea in self.ideas:
            if idea["id"] == idea_id:
                idea["status"] = "planned"
                idea["plan_text"] = plan_text
                break

    def start_idea(self, idea_id: int) -> Optional[dict]:
        for idea in self.ideas:
            if idea["id"] == idea_id:
                idea["status"] = "active"
                return idea
        return None

    def finish_idea(self, idea_id: int, result: str = "") -> None:
        for idea in self.ideas:
            if idea["id"] == idea_id:
                idea["status"] = "done"
                idea["result"] = result[:300] if result else ""
                break

    def get_ideas(self, limit: int = 50) -> list[dict]:
        return self.ideas[:limit]

    # ── Articles (RSS for Яндекс Дзен) ───────────────────────────────────────

    def save_article(self, title: str, content: str) -> dict:
        self._article_counter += 1
        article = {
            "id":         self._article_counter,
            "title":      title,
            "content":    content,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        self.articles.insert(0, article)
        if len(self.articles) > 100:
            self.articles.pop()
        return article

    def get_articles(self, limit: int = 50) -> list[dict]:
        return self.articles[:limit]

    # ── Diary ─────────────────────────────────────────────────────────────────

    async def add_diary_entry(self, agent: str, event_type: str, content: str) -> None:
        if not self.db:
            return
        try:
            await self.db.insert("diary", {
                "agent": agent,
                "event_type": event_type,
                "content": content,
                "created_at": datetime.utcnow().isoformat(),
            })
        except Exception as e:
            print(f"[Supabase] add_diary_entry error: {e}")

    async def get_diary(self, agent: Optional[str] = None, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            params: dict = {
                "select": "id,agent,event_type,content,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            }
            if agent:
                params["agent"] = f"eq.{agent}"
            return await self.db.select("diary", params)
        except Exception as e:
            print(f"[Supabase] get_diary error: {e}")
            return []

    # ── Scheduled Tasks ──────────────────────────────────────────────────────

    async def create_scheduled_task(self, title: str, horizon: str, priority: str) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.insert_returning("scheduled_tasks", {
                "title": title,
                "horizon": horizon,
                "priority": priority,
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] create_scheduled_task error: {e}")
            return None

    async def get_scheduled_tasks(
        self, horizon: Optional[str] = None, status: Optional[str] = None, limit: int = 50,
    ) -> list:
        if not self.db:
            return []
        try:
            params: dict = {
                "select": "id,title,horizon,priority,status,created_at,updated_at",
                "order": "created_at.desc",
                "limit": str(limit),
            }
            if horizon:
                params["horizon"] = f"eq.{horizon}"
            if status:
                params["status"] = f"eq.{status}"
            return await self.db.select("scheduled_tasks", params)
        except Exception as e:
            print(f"[Supabase] get_scheduled_tasks error: {e}")
            return []

    async def update_scheduled_task_status(self, task_id: int, new_status: str) -> bool:
        if not self.db:
            return False
        try:
            await self.db.update("scheduled_tasks", {"id": task_id}, {
                "status": new_status,
                "updated_at": datetime.utcnow().isoformat(),
            })
            return True
        except Exception as e:
            print(f"[Supabase] update_scheduled_task_status error: {e}")
            return False

    # ── Quests ────────────────────────────────────────────────────────────────

    async def create_quest(
        self, title: str, description: str, quest_type: str, agent: str,
        xp_reward: int = 10, data: Optional[dict] = None,
    ) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.insert_returning("quests", {
                "title": title,
                "description": description,
                "quest_type": quest_type,
                "agent": agent,
                "status": "pending",
                "xp_reward": xp_reward,
                "data": data or {},
                "created_at": datetime.utcnow().isoformat(),
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] create_quest error: {e}")
            return None

    async def get_quests(self, status: Optional[str] = None, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            params: dict = {
                "select": "id,title,description,quest_type,agent,status,data,response,xp_reward,created_at,completed_at",
                "order": "created_at.desc",
                "limit": str(limit),
            }
            if status:
                params["status"] = f"eq.{status}"
            return await self.db.select("quests", params)
        except Exception as e:
            print(f"[Supabase] get_quests error: {e}")
            return []

    async def complete_quest(self, quest_id: int, response: Optional[dict] = None) -> bool:
        if not self.db:
            return False
        try:
            update_data: dict = {
                "status": "completed",
                "completed_at": datetime.utcnow().isoformat(),
            }
            if response is not None:
                update_data["response"] = response
            await self.db.update("quests", {"id": quest_id}, update_data)
            return True
        except Exception as e:
            print(f"[Supabase] complete_quest error: {e}")
            return False

    async def get_briefing(self) -> dict:
        """24h summary: pending quests, completed tasks, diary entries, active agents."""
        if not self.db:
            return {"quests_pending": 0, "tasks_completed_24h": 0, "diary_entries_24h": 0,
                    "active_agents_24h": [], "last_diary": []}
        try:
            from datetime import timedelta
            since = (datetime.utcnow() - timedelta(hours=24)).isoformat()

            quests_pending, tasks_24h, diary_24h, last_diary = await asyncio.gather(
                self.db.select("quests", {
                    "select": "id",
                    "status": "eq.pending",
                }),
                self.db.select("tasks", {
                    "select": "id",
                    "status": "eq.done",
                    "finished_at": f"gte.{since}",
                }),
                self.db.select("diary", {
                    "select": "id,agent",
                    "created_at": f"gte.{since}",
                }),
                self.db.select("diary", {
                    "select": "id,agent,event_type,content,created_at",
                    "order": "created_at.desc",
                    "limit": "5",
                }),
            )

            active_agents = list({r["agent"] for r in diary_24h if isinstance(r, dict)})

            return {
                "quests_pending": len(quests_pending) if isinstance(quests_pending, list) else 0,
                "tasks_completed_24h": len(tasks_24h) if isinstance(tasks_24h, list) else 0,
                "diary_entries_24h": len(diary_24h) if isinstance(diary_24h, list) else 0,
                "active_agents_24h": active_agents,
                "last_diary": last_diary if isinstance(last_diary, list) else [],
            }
        except Exception as e:
            print(f"[Supabase] get_briefing error: {e}")
            return {"quests_pending": 0, "tasks_completed_24h": 0, "diary_entries_24h": 0,
                    "active_agents_24h": [], "last_diary": []}

    # ── Public API ────────────────────────────────────────────────────────────

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
