"""
System monitor for Agent Office.
Runs as asyncio background tasks alongside FastAPI.

Features:
- Health check loop: n8n + Supabase connectivity
- Error rate monitoring: alert on high error rate
- Stuck agent detection: alert if agent working too long
- Daily summary report to Telegram
"""

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger("monitor")


class SystemMonitor:
    def __init__(self, state_manager, broadcast_fn, tg_notify_fn, n8n_webhook: str):
        self.state = state_manager
        self.broadcast = broadcast_fn
        self.tg_notify = tg_notify_fn
        self.n8n_url = n8n_webhook.rsplit("/webhook", 1)[0] if "/webhook" in n8n_webhook else ""
        self._alert_cooldown: dict[str, datetime] = {}
        self._running = True

    async def start(self):
        """Launch all monitoring background tasks."""
        logger.info("[Monitor] Starting system monitor")
        asyncio.create_task(self._health_check_loop())
        asyncio.create_task(self._error_rate_monitor())
        asyncio.create_task(self._stuck_agent_detector())
        asyncio.create_task(self._daily_report_loop())

    def stop(self):
        self._running = False

    # ── Health checks ────────────────────────────────────────────────────────

    async def _health_check_loop(self):
        """Every 60s: check n8n and Supabase connectivity."""
        await asyncio.sleep(30)  # initial delay
        while self._running:
            try:
                await self._check_n8n()
                await self._check_db()
            except Exception as e:
                logger.error("[Monitor] health check error: %s", e)
            await asyncio.sleep(60)

    async def _check_n8n(self):
        if not self.n8n_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{self.n8n_url}/healthz")
                if r.status_code != 200:
                    await self._alert("n8n_unhealthy", f"n8n вернул {r.status_code}")
        except httpx.ConnectError:
            await self._alert("n8n_down", "n8n недоступен (connection refused)")
        except httpx.TimeoutException:
            await self._alert("n8n_timeout", "n8n не отвечает (timeout)")
        except Exception as e:
            await self._alert("n8n_error", f"n8n check error: {e}")

    async def _check_db(self):
        if not self.state.db:
            return
        try:
            rows = await self.state.db.select("messages", {"limit": "1"})
            # If returns without error, DB is fine
        except Exception as e:
            await self._alert("db_error", f"Supabase error: {e}")

    # ── Error rate monitor ───────────────────────────────────────────────────

    async def _error_rate_monitor(self):
        """Every 5 min: check if error rate is too high."""
        await asyncio.sleep(60)  # initial delay
        while self._running:
            try:
                if self.state.db:
                    cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()
                    errors = await self.state.db.select("agent_errors", {
                        "created_at": f"gt.{cutoff}",
                        "limit": "20",
                    })
                    if errors and len(errors) >= 3:
                        agents = set(e.get("agent", "?") for e in errors)
                        await self._alert(
                            "high_error_rate",
                            f"{len(errors)} ошибок за 10 мин. Агенты: {', '.join(agents)}"
                        )
            except Exception as e:
                logger.error("[Monitor] error rate check failed: %s", e)
            await asyncio.sleep(300)

    # ── Stuck agent detector ─────────────────────────────────────────────────

    async def _stuck_agent_detector(self):
        """Every 2 min: check for agents stuck in working state."""
        await asyncio.sleep(120)  # initial delay
        while self._running:
            try:
                for key, agent in self.state.agents.items():
                    if agent.status in ("working", "thinking"):
                        last_change = getattr(agent, "last_status_change", "")
                        if last_change:
                            try:
                                changed_at = datetime.fromisoformat(last_change)
                                stuck_mins = (datetime.utcnow() - changed_at).total_seconds() / 60
                                if stuck_mins > 10:
                                    await self._alert(
                                        f"stuck_{key}",
                                        f"{key} в статусе '{agent.status}' уже {stuck_mins:.0f} мин"
                                    )
                            except ValueError:
                                pass
            except Exception as e:
                logger.error("[Monitor] stuck check failed: %s", e)
            await asyncio.sleep(120)

    # ── Daily report ─────────────────────────────────────────────────────────

    async def _daily_report_loop(self):
        """Send daily summary at ~midnight UTC."""
        while self._running:
            now = datetime.utcnow()
            # Next midnight
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_sec = (tomorrow - now).total_seconds()
            await asyncio.sleep(wait_sec)

            if not self._running:
                break

            try:
                report = await self._generate_daily_report()
                if report:
                    await self.tg_notify(report)
            except Exception as e:
                logger.error("[Monitor] daily report failed: %s", e)

    async def _generate_daily_report(self) -> str:
        """Generate daily summary."""
        if not self.state.db:
            return ""

        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()

        # Count completed tasks
        tasks = await self.state.db.select("scheduled_tasks", {
            "status": "eq.done",
            "limit": "100",
        })
        done_24h = 0
        if tasks:
            for t in tasks:
                created = t.get("created_at", "")
                if created > cutoff:
                    done_24h += 1

        # Count errors
        errors = await self.state.db.select("agent_errors", {
            "created_at": f"gt.{cutoff}",
        })
        error_count = len(errors) if errors else 0

        # Pending quests
        quests = await self.state.db.select("quests", {
            "status": "eq.pending",
        })
        pending_quests = len(quests) if quests else 0

        return (
            f"📊 <b>Дневной отчёт</b>\n\n"
            f"✅ Задач завершено: {done_24h}\n"
            f"💥 Ошибок: {error_count}\n"
            f"⭐ Pending квестов: {pending_quests}\n"
            f"🕐 Uptime: работает"
        )

    # ── Alert system ─────────────────────────────────────────────────────────

    async def _alert(self, alert_type: str, message: str):
        """Send alert with 15-min cooldown per alert type."""
        now = datetime.utcnow()
        last = self._alert_cooldown.get(alert_type)
        if last and (now - last).total_seconds() < 900:
            return  # cooldown active
        self._alert_cooldown[alert_type] = now
        logger.warning("[ALERT] %s: %s", alert_type, message)
        try:
            await self.tg_notify(f"🚨 <b>Alert</b>: {alert_type}\n{message}")
        except Exception as e:
            logger.error("[Monitor] alert notify failed: %s", e)
