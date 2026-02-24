"""
Pure state manager for n8n-based agent orchestration.
All AI logic lives in n8n workflows; this module only tracks agent states
and provides WebSocket broadcast helpers.
"""
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable


# ── Agent catalogue ───────────────────────────────────────────────────────────
AGENT_DEFS = {
    "manager":    {"id": 0, "name": "Manager",    "role": "Orchestrator",   "emoji": "🎯", "color": "#a78bfa"},
    "researcher": {"id": 1, "name": "Researcher", "role": "Web Researcher", "emoji": "🔍", "color": "#38bdf8"},
    "writer":     {"id": 2, "name": "Writer",     "role": "Content Writer", "emoji": "✍️", "color": "#4ade80"},
    "coder":      {"id": 3, "name": "Coder",      "role": "Code Engineer",  "emoji": "💻", "color": "#facc15"},
    "analyst":    {"id": 4, "name": "Analyst",    "role": "Data Analyst",   "emoji": "📊", "color": "#fb7185"},
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


# ── State manager ─────────────────────────────────────────────────────────────
class StateManager:
    def __init__(self):
        self.agents: dict[str, AgentState] = {
            k: AgentState(key=k, **v) for k, v in AGENT_DEFS.items()
        }
        self.history: list[dict] = []

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

        # Update agent state
        if "status" in payload:
            agent.status = payload["status"]
        if "task" in payload:
            agent.task = payload["task"][:120]
        if "progress" in payload:
            agent.progress = int(payload["progress"])

        await broadcast({"type": "agents", "agents": self.agent_states()})

        # Optionally push a chat message
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
            await broadcast({"type": "chat", "message": msg})

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
        return msg
