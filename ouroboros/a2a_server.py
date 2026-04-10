"""
A2A — Server module.

Runs a separate Starlette/uvicorn server on A2A_PORT (default 18800).
Serves the dynamic Agent Card and handles A2A JSON-RPC requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pathlib
import re
import socket
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from ouroboros.a2a_executor import OuroborosA2AExecutor
from ouroboros.a2a_task_store import FileTaskStore

log = logging.getLogger("a2a-server")

# Module-level server reference for lifecycle management
_server: Optional[uvicorn.Server] = None
_cleanup_task: Optional[asyncio.Task] = None


def _setup_logging(data_dir: pathlib.Path) -> None:
    """Configure A2A server logging."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "a2a.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    a2a_log = logging.getLogger("a2a-server")
    if not a2a_log.handlers:
        a2a_log.addHandler(handler)
        a2a_log.addHandler(logging.StreamHandler())
        a2a_log.setLevel(logging.INFO)


def _resolve_host(configured_host: str) -> str:
    """Resolve 0.0.0.0 to an actual hostname for the Agent Card URL."""
    if configured_host in ("0.0.0.0", "::"):
        try:
            return socket.getfqdn() or socket.gethostname() or "localhost"
        except Exception:
            return "localhost"
    return configured_host


def _parse_identity(data_dir: pathlib.Path) -> tuple:
    """Extract name and description from identity.md. Returns (name, description)."""
    identity_path = data_dir / "memory" / "identity.md"
    if not identity_path.exists():
        return "", ""
    try:
        content = identity_path.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        name = ""
        heading_found = False
        desc_lines = []
        for line in lines:
            if not heading_found and line.startswith("# "):
                heading_found = True
                # Try to extract name from heading: "# I Am Ouroboros" -> "Ouroboros"
                raw = line.lstrip("# ").strip()
                candidate = re.sub(r"^I\s+Am\s+", "", raw, flags=re.IGNORECASE).strip()
                # If heading is generic (e.g. "Who I Am"), skip — look for name in body
                if candidate.lower() not in ("who i am", "about me", "identity"):
                    name = candidate
                continue
            if heading_found and not desc_lines and not line.strip():
                continue
            if heading_found and line.startswith("---"):
                break
            if heading_found and line.startswith("## "):
                break
            if heading_found and line.strip():
                # Try to extract name from first line: "I'm Ouroboros." or "I am Ouroboros."
                if not name:
                    m = re.match(r"^I(?:'m|\s+am)\s+(\w+)", line.strip(), re.IGNORECASE)
                    if m:
                        name = m.group(1)
                desc_lines.append(line.strip())
                if len(desc_lines) >= 3:
                    break
        desc = " ".join(desc_lines)
        return name, desc
    except Exception:
        return "", ""


def _build_skills_from_registry() -> List[AgentSkill]:
    """Build A2A skills from the ToolRegistry."""
    try:
        from supervisor.workers import _get_chat_agent
        agent = _get_chat_agent()
        registry = agent.tools  # OuroborosAgent.tools is a ToolRegistry
        skills = []
        for schema_item in registry.schemas():
            func = schema_item.get("function", {})
            name = func.get("name", "")
            desc = func.get("description", "")
            if not name:
                continue
            prefix = name.split("_")[0] if "_" in name else "tool"
            skills.append(AgentSkill(
                id=name,
                name=name,
                description=desc[:200] if desc else "",
                tags=[prefix],
            ))
        return skills
    except Exception:
        log.debug("ToolRegistry not available yet, using fallback skills", exc_info=True)
        return [
            AgentSkill(
                id="general",
                name="General Assistant",
                description="Code editing, analysis, git operations, web search, file management",
                tags=["general"],
            )
        ]


def _build_agent_card(settings: Dict[str, Any], host: str, port: int) -> AgentCard:
    """Build a dynamic AgentCard from settings, identity.md, and ToolRegistry."""
    from ouroboros import get_version
    from ouroboros.config import DATA_DIR

    # Name and description
    id_name, id_desc = _parse_identity(DATA_DIR)
    name = settings.get("A2A_AGENT_NAME", "").strip() or id_name or "Ouroboros"
    description = (
        settings.get("A2A_AGENT_DESCRIPTION", "").strip()
        or id_desc
        or "Self-modifying AI agent"
    )

    resolved_host = _resolve_host(host)
    url = f"http://{resolved_host}:{port}/"

    skills = _build_skills_from_registry()

    return AgentCard(
        name=name,
        description=description,
        url=url,
        version=get_version(),
        skills=skills,
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


async def _task_cleanup_loop(store: FileTaskStore, interval_sec: int = 3600) -> None:
    """Periodically clean up expired tasks."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            removed = await store.cleanup_expired()
            if removed:
                log.info("A2A task cleanup: removed %d expired tasks", removed)
        except Exception:
            log.warning("A2A task cleanup error", exc_info=True)


async def start_a2a_server(settings: Dict[str, Any]) -> None:
    """Start the A2A server as an async task."""
    global _server, _cleanup_task

    from ouroboros.config import DATA_DIR

    host = str(settings.get("A2A_HOST", "127.0.0.1")).strip()
    port = int(settings.get("A2A_PORT", 18800))
    max_concurrent = int(settings.get("A2A_MAX_CONCURRENT", 3))
    ttl_hours = int(settings.get("A2A_TASK_TTL_HOURS", 24))

    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "A2A server binding to %s without authentication. "
            "Set A2A_HOST=127.0.0.1 or configure auth for production use.",
            host,
        )

    _setup_logging(DATA_DIR)
    log.info("Starting A2A server on %s:%d", host, port)

    # Build components
    task_store = FileTaskStore(DATA_DIR, ttl_hours=ttl_hours)
    executor = OuroborosA2AExecutor(max_concurrent=max_concurrent)
    agent_card = _build_agent_card(settings, host, port)

    def _refresh_skills(card: AgentCard) -> AgentCard:
        """Update skills from ToolRegistry on each Agent Card request."""
        live_skills = _build_skills_from_registry()
        if live_skills and live_skills[0].id != "general":
            card.skills = live_skills
        return card

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=handler,
        card_modifier=_refresh_skills,
    )
    starlette_app = a2a_app.build()

    # Start cleanup loop
    _cleanup_task = asyncio.create_task(
        _task_cleanup_loop(task_store), name="a2a-task-cleanup"
    )

    # Run uvicorn
    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    await _server.serve()


def stop_a2a_server() -> None:
    """Signal the A2A server to shut down."""
    global _server, _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None
    if _server:
        _server.should_exit = True
        _server = None
    log.info("A2A server shutdown requested")
