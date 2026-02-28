"""
State manager for n8n-based agent orchestration.
Persists all data to Supabase via REST API.
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
    "qa":         {"id": 5, "name": "QA",         "role": "Quality Assurance", "emoji": "🛡️", "color": "#f59e0b"},
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
    last_status_change: str = ""

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
            if r.status_code in (200, 201):
                return r.json()
            import logging
            logging.getLogger("agent-office").error(
                f"[Supabase] INSERT {table} → {r.status_code}: {r.text[:300]}"
            )
            return []

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

    async def upsert(self, table: str, data: dict, on_conflict: str = "") -> list:
        headers = {**self.headers, "Prefer": "return=representation,resolution=merge-duplicates"}
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{self.url}/rest/v1/{table}",
                headers=headers,
                json=data,
            )
            return r.json() if r.status_code in (200, 201) else []

    async def delete(self, table: str, match: dict) -> None:
        params = {k: f"eq.{v}" for k, v in match.items()}
        async with httpx.AsyncClient(timeout=10) as client:
            await client.delete(
                f"{self.url}/rest/v1/{table}",
                headers=self.headers,
                params=params,
            )


# ── State manager ─────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self, supabase_url: Optional[str] = None, supabase_key: Optional[str] = None):
        self.agents: dict[str, AgentState] = {
            k: AgentState(key=k, **v) for k, v in AGENT_DEFS.items()
        }
        self.history: list[dict] = []
        self._current_task_id: Optional[int] = None
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
                "select": "id,created_at,content,status,summary,finished_at,assigned_agent,priority,tags",
                "order": "created_at.desc",
                "limit": str(limit),
            })
        except Exception as e:
            print(f"[Supabase] get_tasks error: {e}")
            return []

    async def get_agent_task_by_id(self, task_id: int) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.select("tasks", {
                "select": "id,created_at,content,status,summary,finished_at,assigned_agent,priority,tags,result,action_items,review_status",
                "id": f"eq.{task_id}",
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] get_agent_task_by_id error: {e}")
            return None

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
            from datetime import datetime
            agent.last_status_change = datetime.utcnow().isoformat()
        if "task" in payload:
            agent.task = payload["task"][:120]
        if "progress" in payload:
            try:
                agent.progress = int(payload["progress"])
            except (ValueError, TypeError):
                pass

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

        # Use taskId from payload if available (fixes race condition with concurrent tasks)
        task_id = payload.get("taskId") or self._current_task_id

        # Handle error status from agents
        if payload.get("status") == "error" and task_id and self.db:
            error_msg = payload.get("message", "Unknown error")
            try:
                await self.db.update("tasks", {"id": task_id}, {
                    "status": "error",
                    "summary": f"Agent error: {error_msg[:500]}",
                })
            except Exception as e:
                print(f"[apply_callback] error marking task {task_id} as error: {e}")
            await broadcast({"type": "tasks_update"})

        # When manager goes idle, mark current task and idea as done
        if key == "manager" and payload.get("status") == "idle":
            self._current_task_id = None
            summary = payload.get("message", "")
            if task_id:
                asyncio.create_task(self.finish_task(task_id, summary))
            elif self.db:
                asyncio.create_task(self._finish_latest_processing(summary))
            idea_id = self._current_idea_id
            self._current_idea_id = None
            if idea_id:
                asyncio.create_task(self.finish_idea(idea_id, summary))

    # ── Ideas board (Supabase) ─────────────────────────────────────────────────

    async def create_idea(self, content: str) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.insert_returning("ideas", {
                "content": content,
                "status": "planning",
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] create_idea error: {e}")
            return None

    async def update_idea_plan(self, idea_id: int, plan_text: str) -> None:
        if not self.db:
            return
        try:
            await self.db.update("ideas", {"id": idea_id}, {
                "status": "planned",
                "plan_text": plan_text,
            })
        except Exception as e:
            print(f"[Supabase] update_idea_plan error: {e}")

    async def start_idea(self, idea_id: int) -> Optional[dict]:
        if not self.db:
            return None
        try:
            await self.db.update("ideas", {"id": idea_id}, {"status": "active"})
            rows = await self.db.select("ideas", {
                "select": "id,content,status,plan_text,result,created_at",
                "id": f"eq.{idea_id}",
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] start_idea error: {e}")
            return None

    async def finish_idea(self, idea_id: int, result: str = "") -> None:
        if not self.db:
            return
        try:
            await self.db.update("ideas", {"id": idea_id}, {
                "status": "done",
                "result": result[:300] if result else "",
            })
        except Exception as e:
            print(f"[Supabase] finish_idea error: {e}")

    async def get_ideas(self, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            return await self.db.select("ideas", {
                "select": "id,content,status,plan_text,result,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            })
        except Exception as e:
            print(f"[Supabase] get_ideas error: {e}")
            return []

    # ── Articles (Supabase, RSS for Яндекс Дзен) ─────────────────────────────

    async def save_article(self, title: str, content: str) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.insert_returning("articles", {
                "title": title,
                "content": content,
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] save_article error: {e}")
            return None

    async def get_articles(self, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            return await self.db.select("articles", {
                "select": "id,title,content,published_url,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            })
        except Exception as e:
            print(f"[Supabase] get_articles error: {e}")
            return []

    async def get_article_by_id(self, article_id: int) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.select("articles", {
                "select": "id,title,content,published_url,created_at",
                "id": f"eq.{article_id}",
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] get_article_by_id error: {e}")
            return None

    # ── Agent Memory ──────────────────────────────────────────────────────────

    async def save_memory(self, agent: str, memory_type: str, content: str,
                          source_task_id: Optional[int] = None,
                          importance: int = 5, tags: list[str] | None = None) -> Optional[dict]:
        if not self.db:
            return None
        try:
            data = {
                "agent": agent,
                "memory_type": memory_type,
                "content": content,
                "importance": importance,
                "tags": tags or [],
            }
            if source_task_id:
                data["source_task_id"] = source_task_id
            rows = await self.db.insert_returning("agent_memory", data)
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] save_memory error: {e}")
            return None

    async def get_memory(self, agent: Optional[str] = None,
                         memory_type: Optional[str] = None,
                         limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            params: dict = {
                "select": "id,agent,memory_type,content,source_task_id,importance,usage_count,tags,created_at",
                "order": "importance.desc,created_at.desc",
                "limit": str(limit),
            }
            if agent:
                params["agent"] = f"eq.{agent}"
            if memory_type:
                params["memory_type"] = f"eq.{memory_type}"
            return await self.db.select("agent_memory", params)
        except Exception as e:
            print(f"[Supabase] get_memory error: {e}")
            return []

    async def get_memory_context(self, agent: str, limit: int = 20) -> list:
        """Top memories for an agent: system map + own lessons + shared high-importance + user prefs."""
        if not self.db:
            return []
        try:
            system_mem, own, shared, prefs = await asyncio.gather(
                # System memories (ecosystem map, global anti-patterns) — always first
                self.db.select("agent_memory", {
                    "select": "id,memory_type,content,importance,tags",
                    "agent": "eq.system",
                    "order": "importance.desc",
                    "limit": "5",
                }),
                self.db.select("agent_memory", {
                    "select": "id,memory_type,content,importance,tags",
                    "agent": f"eq.{agent}",
                    "order": "importance.desc,created_at.desc",
                    "limit": str(min(limit, 15)),
                }),
                self.db.select("agent_memory", {
                    "select": "id,memory_type,content,importance,tags,agent",
                    "agent": f"neq.{agent}",
                    "importance": "gte.8",
                    "order": "importance.desc,created_at.desc",
                    "limit": "5",
                }),
                self.db.select("user_profile", {
                    "select": "category,key,value",
                    "order": "category.asc",
                }),
            )
            # Filter out system from shared
            shared = [m for m in shared if m.get("agent") != "system"]
            # Tag shared lessons
            for item in shared:
                item["shared_from"] = item.pop("agent", "")
            context = system_mem + own + shared
            if prefs:
                context.append({
                    "id": 0,
                    "memory_type": "user_preferences",
                    "content": "; ".join(f"{p['key']}: {p['value']}" for p in prefs),
                    "importance": 10,
                    "tags": ["profile"],
                })
            return context
        except Exception as e:
            print(f"[Supabase] get_memory_context error: {e}")
            return []

    async def delete_memory(self, memory_id: int) -> bool:
        if not self.db:
            return False
        try:
            await self.db.delete("agent_memory", {"id": memory_id})
            return True
        except Exception as e:
            print(f"[Supabase] delete_memory error: {e}")
            return False

    # ── User Profile ──────────────────────────────────────────────────────────

    async def get_profile(self) -> list:
        if not self.db:
            return []
        try:
            return await self.db.select("user_profile", {
                "select": "id,category,key,value,confidence,source,created_at",
                "order": "category.asc,key.asc",
            })
        except Exception as e:
            print(f"[Supabase] get_profile error: {e}")
            return []

    async def update_profile(self, category: str, key: str, value: str,
                             source: str = "explicit") -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.upsert("user_profile", {
                "category": category,
                "key": key,
                "value": value,
                "source": source,
                "confidence": 1.0 if source == "explicit" else 0.5,
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] update_profile error: {e}")
            return None

    # ── Task Feedback ─────────────────────────────────────────────────────────

    async def save_feedback(self, task_id: int, agent: str,
                            rating: int, comment: str = "") -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.insert_returning("task_feedback", {
                "task_id": task_id,
                "agent": agent,
                "rating": rating,
                "comment": comment,
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] save_feedback error: {e}")
            return None

    # ── Agent Errors ──────────────────────────────────────────────────────────

    async def save_error(self, agent: str, error_type: str, error_detail: str,
                         task_id: Optional[int] = None) -> Optional[dict]:
        if not self.db:
            return None
        try:
            data = {
                "agent": agent,
                "error_type": error_type,
                "error_detail": error_detail,
            }
            if task_id:
                data["task_id"] = task_id
            rows = await self.db.insert_returning("agent_errors", data)
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] save_error error: {e}")
            return None

    async def get_errors(self, agent: Optional[str] = None, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            params: dict = {
                "select": "id,agent,task_id,error_type,error_detail,reflection,lesson,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            }
            if agent:
                params["agent"] = f"eq.{agent}"
            return await self.db.select("agent_errors", params)
        except Exception as e:
            print(f"[Supabase] get_errors error: {e}")
            return []

    async def update_error_reflection(self, error_id: int, reflection: str, lesson: str) -> bool:
        if not self.db:
            return False
        try:
            await self.db.update("agent_errors", {"id": error_id}, {
                "reflection": reflection,
                "lesson": lesson,
            })
            return True
        except Exception as e:
            print(f"[Supabase] update_error_reflection error: {e}")
            return False

    async def get_agent_stats(self) -> list:
        """Per-agent stats: tasks completed, avg rating, memory count, error count."""
        if not self.db:
            return []
        try:
            feedback, memories, errors, tasks = await asyncio.gather(
                self.db.select("task_feedback", {
                    "select": "agent,rating",
                }),
                self.db.select("agent_memory", {
                    "select": "agent,id",
                }),
                self.db.select("agent_errors", {
                    "select": "agent,id",
                }),
                self.db.select("tasks", {
                    "select": "id,status,assigned_agent",
                }),
            )
            stats = []
            for key, defn in AGENT_DEFS.items():
                agent_fb = [f for f in feedback if isinstance(f, dict) and f.get("agent") == key]
                agent_mem = [m for m in memories if isinstance(m, dict) and m.get("agent") == key]
                agent_err = [e for e in errors if isinstance(e, dict) and e.get("agent") == key]
                agent_tasks = [t for t in tasks if isinstance(t, dict) and t.get("assigned_agent") == key]
                ratings = [f["rating"] for f in agent_fb if f.get("rating")]
                stats.append({
                    "agent": key,
                    "name": defn["name"],
                    "emoji": defn["emoji"],
                    "color": defn["color"],
                    "role": defn["role"],
                    "tasks_count": len(agent_tasks),
                    "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
                    "ratings_count": len(ratings),
                    "memory_count": len(agent_mem),
                    "error_count": len(agent_err),
                })
            return stats
        except Exception as e:
            print(f"[Supabase] get_agent_stats error: {e}")
            return []

    async def get_feedback(self, agent: Optional[str] = None, limit: int = 50) -> list:
        if not self.db:
            return []
        try:
            params: dict = {
                "select": "id,task_id,agent,rating,comment,created_at",
                "order": "created_at.desc",
                "limit": str(limit),
            }
            if agent:
                params["agent"] = f"eq.{agent}"
            return await self.db.select("task_feedback", params)
        except Exception as e:
            print(f"[Supabase] get_feedback error: {e}")
            return []

    # ── Direct Chat ──────────────────────────────────────────────────────────

    async def save_direct_message(self, agent: str, role: str, content: str) -> None:
        """Save a direct chat message. role is 'direct_user' or 'direct_agent'."""
        if not self.db:
            return
        try:
            agent_def = AGENT_DEFS.get(agent, {})
            await self.db.insert("messages", {
                "role": role,
                "name": agent,
                "emoji": agent_def.get("emoji", "🤖") if role == "direct_agent" else "👤",
                "color": agent_def.get("color", "#64748b") if role == "direct_agent" else "#6366f1",
                "content": content,
                "msg_time": datetime.now().strftime("%H:%M"),
            })
        except Exception as e:
            print(f"[Supabase] save_direct_message error: {e}")

    async def get_direct_messages(self, agent: str, limit: int = 30) -> list:
        """Get direct chat history for an agent."""
        if not self.db:
            return []
        try:
            user_msgs, agent_msgs = await asyncio.gather(
                self.db.select("messages", {
                    "select": "role,content,created_at",
                    "name": f"eq.{agent}",
                    "role": "eq.direct_user",
                    "order": "created_at.desc",
                    "limit": str(limit),
                }),
                self.db.select("messages", {
                    "select": "role,content,created_at",
                    "name": f"eq.{agent}",
                    "role": "eq.direct_agent",
                    "order": "created_at.desc",
                    "limit": str(limit),
                }),
            )
            all_msgs = [m for m in (user_msgs + agent_msgs) if isinstance(m, dict)]
            all_msgs.sort(key=lambda x: x.get("created_at", ""))
            all_msgs = all_msgs[-limit:]
            return [
                {
                    "role": "user" if r["role"] == "direct_user" else "assistant",
                    "content": r["content"],
                    "created_at": r.get("created_at", ""),
                }
                for r in all_msgs
            ]
        except Exception as e:
            print(f"[Supabase] get_direct_messages error: {e}")
            return []

    # ── Analytics ────────────────────────────────────────────────────────────

    async def get_analytics_overview(self, days: int = 7) -> dict:
        """Aggregate stats for the analytics dashboard."""
        if not self.db:
            return {}
        try:
            from datetime import timedelta
            since = (datetime.utcnow() - timedelta(days=days)).isoformat()

            tasks, feedback, errors, memories = await asyncio.gather(
                self.db.select("tasks", {
                    "select": "id,status,created_at,finished_at",
                    "created_at": f"gte.{since}",
                }),
                self.db.select("task_feedback", {
                    "select": "rating,created_at",
                    "created_at": f"gte.{since}",
                }),
                self.db.select("agent_errors", {
                    "select": "id,created_at",
                    "created_at": f"gte.{since}",
                }),
                self.db.select("agent_memory", {
                    "select": "id,created_at",
                    "created_at": f"gte.{since}",
                }),
            )

            done_tasks = [t for t in tasks if isinstance(t, dict) and t.get("status") == "done"]
            ratings = [f["rating"] for f in feedback if isinstance(f, dict) and f.get("rating")]

            tasks_by_day: dict[str, int] = {}
            for t in tasks:
                if isinstance(t, dict) and t.get("created_at"):
                    day = t["created_at"][:10]
                    tasks_by_day[day] = tasks_by_day.get(day, 0) + 1

            return {
                "period_days": days,
                "total_tasks": len(tasks),
                "done_tasks": len(done_tasks),
                "avg_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
                "ratings_count": len(ratings),
                "total_errors": len(errors),
                "total_memories": len(memories),
                "tasks_by_day": dict(sorted(tasks_by_day.items())),
            }
        except Exception as e:
            print(f"[Supabase] get_analytics_overview error: {e}")
            return {}

    # ── Delete Profile Entry ─────────────────────────────────────────────────

    async def delete_profile(self, profile_id: int) -> bool:
        if not self.db:
            return False
        try:
            await self.db.delete("user_profile", {"id": profile_id})
            return True
        except Exception as e:
            print(f"[Supabase] delete_profile error: {e}")
            return False

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
                "select": "id,title,horizon,priority,status,result,action_items,review_status,assigned_agent,linked_task_id,created_at,updated_at",
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

    async def get_scheduled_task_by_id(self, task_id: int) -> Optional[dict]:
        if not self.db:
            return None
        try:
            rows = await self.db.select("scheduled_tasks", {
                "select": "id,title,horizon,priority,status,result,action_items,review_status,assigned_agent,linked_task_id,created_at,updated_at",
                "id": f"eq.{task_id}",
            })
            return rows[0] if rows else None
        except Exception as e:
            print(f"[Supabase] get_scheduled_task_by_id error: {e}")
            return None

    async def update_scheduled_task(self, task_id: int, data: dict) -> bool:
        if not self.db:
            return False
        try:
            data["updated_at"] = datetime.utcnow().isoformat()
            await self.db.update("scheduled_tasks", {"id": task_id}, data)
            return True
        except Exception as e:
            print(f"[Supabase] update_scheduled_task error: {e}")
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
