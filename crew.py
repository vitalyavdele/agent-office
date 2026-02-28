"""
crew.py — CrewAI multi-agent team for Agent Office.

Each agent has a real role, goal, backstory, and tools (not just a system prompt).
Agents in a multi-agent crew share context and build on each other's work.

Endpoints:
  POST /api/crew/execute
    - {agent: "researcher", task: "..."}       → single-agent crew (backward compat)
    - {agents: ["researcher", "writer", "deployer"], task: "..."} → collaborative crew
"""

import os
import functools
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import httpx
from crewai import Agent, Task, Crew, Process, LLM
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
PUBLISH_URL    = os.getenv("DASHBOARD_URL", "https://office.mopofipofue.beget.app") + "/api/articles"


# ── Custom Tool: Publish Article ──────────────────────────────────────────────

class _PublishInput(BaseModel):
    title:   str = Field(description="The article title")
    content: str = Field(description="Full article content in markdown")


class PublishArticleTool(BaseTool):
    name: str = "publish_article"
    description: str = (
        "Publish a completed article to the content platform and get its public URL. "
        "Only call this when the article is fully written and ready for publication."
    )
    args_schema: type[BaseModel] = _PublishInput

    def _run(self, title: str, content: str) -> str:
        try:
            r = httpx.post(
                PUBLISH_URL,
                json={"title": title, "content": content},
                timeout=30,
            )
            data = r.json()
            url = data.get("article_url", "N/A")
            return f"Article published successfully! Public URL: {url}"
        except Exception as e:
            return f"Failed to publish article: {e}"


# ── Optional: Tavily web search ───────────────────────────────────────────────

def _search_tools() -> list:
    """Return list of search tools if Tavily is configured."""
    if not TAVILY_API_KEY:
        return []
    try:
        from crewai_tools import TavilySearchResults
        return [TavilySearchResults(max_results=5)]
    except Exception:
        return []


# ── LLM factory ───────────────────────────────────────────────────────────────

def _llm() -> LLM:
    return LLM(model="gpt-4o", api_key=OPENAI_API_KEY, temperature=0.7)


# ── Agent definitions ─────────────────────────────────────────────────────────

def _build_agents() -> dict[str, Agent]:
    llm    = _llm()
    search = _search_tools()

    researcher = Agent(
        role="Senior Research Analyst",
        goal=(
            "Find accurate, up-to-date information from real web sources "
            "and produce structured research reports with cited URLs."
        ),
        backstory=(
            "You are an expert researcher with 10 years of experience in information synthesis. "
            "You never fabricate facts — if you don't know, you search first. "
            "Your reports always include source URLs and are structured: "
            "Summary → Key Findings → Sources."
        ),
        tools=search,
        llm=llm,
        verbose=True,
        max_iter=5,
        allow_delegation=False,
    )

    writer = Agent(
        role="Content Writer — Яндекс Дзен Specialist",
        goal="Write engaging, well-structured articles of 800–1500 words that readers genuinely enjoy.",
        backstory=(
            "Experienced content writer specializing in Яндекс Дзен format. "
            "Every article you write has: a hook intro that grabs attention, "
            "clear sections with ## headers, and a memorable conclusion. "
            "Style: conversational, informative, no empty filler words. "
            "You always match the language of the task (Russian for Russian tasks)."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    coder = Agent(
        role="Senior Software Engineer",
        goal="Write clean, working, production-ready code with concise explanations.",
        backstory=(
            "10+ years of full-stack experience across Python, JavaScript, and web technologies. "
            "You write secure, maintainable code. Always use proper markdown code blocks "
            "with language tags. Explain key architectural decisions briefly. "
            "You can search documentation when needed."
        ),
        tools=search,
        llm=llm,
        verbose=True,
        max_iter=5,
        allow_delegation=False,
    )

    analyst = Agent(
        role="Business Analyst",
        goal="Analyze situations and data to produce specific, actionable insights and recommendations.",
        backstory=(
            "Data-driven analyst with expertise in business intelligence and strategic consulting. "
            "Structure: ## Situation → ## Analysis → ## Key Insights (bullets) → ## Recommendations. "
            "Always quantify where possible. Use real market data when available."
        ),
        tools=search,
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    ux_auditor = Agent(
        role="UX/UI Auditor",
        goal="Identify UX problems and provide prioritized, concrete improvement recommendations.",
        backstory=(
            "Senior UX researcher with background in conversion rate optimization. "
            "Rate every issue: 🔴 Critical / 🟡 Major / 🟢 Minor. "
            "For each issue: name the exact element, describe the problem clearly, suggest the fix."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    site_coder = Agent(
        role="Frontend Developer",
        goal="Build complete, immediately copy-paste-ready web pages and UI components.",
        backstory=(
            "Expert in modern frontend: CSS flexbox/grid, semantic HTML5, vanilla JavaScript. "
            "No external dependencies unless the user explicitly asks for them. "
            "Code is always complete, well-commented, and immediately runnable."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    deployer = Agent(
        role="Content Publisher",
        goal="Publish completed articles to the platform and confirm success with the public URL.",
        backstory=(
            "You handle the final publishing step. You take the finished article content, "
            "use the publish_article tool to post it, and always confirm the public URL. "
            "If content is not ready, ask the writer to finish first."
        ),
        tools=[PublishArticleTool()],
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    return {
        "researcher": researcher,
        "writer":     writer,
        "coder":      coder,
        "analyst":    analyst,
        "ux-auditor": ux_auditor,
        "site-coder": site_coder,
        "deployer":   deployer,
    }


# ── Crew runner ───────────────────────────────────────────────────────────────

def run_crew(
    task_description: str,
    agent_name:  Optional[str]       = None,
    agent_names: Optional[list[str]] = None,
    context:     str                 = "",
) -> str:
    """
    Run a CrewAI crew synchronously (call from asyncio via run_in_executor).

    Modes:
      agent_name only   → single-agent crew (backward compat with /api/agent/execute)
      agent_names list  → multi-agent sequential crew (agents collaborate in order)
    """
    agents_dict = _build_agents()

    if agent_name and not agent_names:
        # ── Single agent mode ──────────────────────────────────────────────────
        agent = agents_dict.get(agent_name)
        if not agent:
            raise ValueError(f"Unknown agent: {agent_name}")

        full_task = task_description
        if context:
            full_task = f"Context:\n{context}\n\nTask:\n{task_description}"

        task = Task(
            description=full_task,
            expected_output="A complete, high-quality response to the task.",
            agent=agent,
        )
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            verbose=True,
        )

    elif agent_names:
        # ── Multi-agent collaborative mode ────────────────────────────────────
        selected = [agents_dict[n] for n in agent_names if n in agents_dict]
        if not selected:
            raise ValueError(f"No valid agents found in: {agent_names}")

        tasks = []
        prev_task: Optional[Task] = None

        for i, agent in enumerate(selected):
            if i == 0:
                desc = task_description
                if context:
                    desc = f"Context:\n{context}\n\nTask:\n{task_description}"
            else:
                desc = (
                    f"Continue the work based on what the previous agent produced. "
                    f"Overall task: {task_description}"
                )

            t = Task(
                description=desc,
                expected_output=f"High-quality output from {agent.role} that advances the overall task.",
                agent=agent,
                context=[prev_task] if prev_task else [],
            )
            tasks.append(t)
            prev_task = t

        crew = Crew(
            agents=selected,
            tasks=tasks,
            process=Process.sequential,
            verbose=True,
        )

    else:
        raise ValueError("Specify either 'agent' (single) or 'agents' (list) parameter.")

    result = crew.kickoff()
    return str(result)
