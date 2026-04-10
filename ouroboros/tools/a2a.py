"""A2A client tools: discover, send, check status of other A2A agents."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger("a2a-server")


def _a2a_discover(ctx: ToolContext, url: str) -> str:
    """Fetch and summarize an A2A agent's Agent Card."""
    import httpx

    base = url.rstrip("/")
    card_url = f"{base}/.well-known/agent.json"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(card_url)
            resp.raise_for_status()
            card = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code} from {card_url}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch agent card: {e}"})

    skills = card.get("skills", [])
    skill_list = []
    for s in skills:
        name = s.get("name", s.get("id", "unknown"))
        desc = s.get("description", "")
        skill_list.append({"name": name, "description": desc})

    result = {
        "name": card.get("name", ""),
        "description": card.get("description", ""),
        "version": card.get("version", ""),
        "url": card.get("url", base),
        "capabilities": card.get("capabilities", {}),
        "skills": skill_list,
        "input_modes": card.get("defaultInputModes", []),
        "output_modes": card.get("defaultOutputModes", []),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _a2a_send(
    ctx: ToolContext,
    url: str,
    message: str,
    task_id: str = "",
    context_id: str = "",
) -> str:
    """Send a message to an A2A agent via JSON-RPC SendMessage."""
    import httpx

    base = url.rstrip("/")
    msg_id = uuid.uuid4().hex

    # Build the JSON-RPC request
    params: Dict[str, Any] = {
        "message": {
            "messageId": msg_id,
            "role": "user",
            "parts": [{"kind": "text", "text": message}],
        },
    }
    if task_id:
        params["message"]["taskId"] = task_id
    if context_id:
        params["message"]["contextId"] = context_id

    payload = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "message/send",
        "params": params,
    }

    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(base + "/", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"Request failed: {e}"})

    if "error" in data:
        return json.dumps({"error": data["error"]}, ensure_ascii=False, indent=2)

    result = data.get("result", {})

    # Result can be a Task or a Message
    task_id_out = result.get("id", "")
    status = result.get("status", {})
    state = status.get("state", "")

    # Extract response text from artifacts or status message
    response_text = ""
    artifacts = result.get("artifacts", [])
    for art in artifacts:
        for part in art.get("parts", []):
            if "text" in part:
                response_text += part["text"]

    # If no artifacts, check if it's a direct message response
    if not response_text and "parts" in result:
        for part in result.get("parts", []):
            if "text" in part:
                response_text += part["text"]

    output = {
        "task_id": task_id_out,
        "status": state,
        "response": response_text or None,
    }
    if result.get("contextId"):
        output["context_id"] = result["contextId"]

    return json.dumps(output, ensure_ascii=False, indent=2)


def _a2a_status(ctx: ToolContext, url: str, task_id: str) -> str:
    """Check the status of an A2A task via JSON-RPC GetTask."""
    import httpx

    base = url.rstrip("/")
    req_id = uuid.uuid4().hex

    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tasks/get",
        "params": {"id": task_id},
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(base + "/", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"Request failed: {e}"})

    if "error" in data:
        return json.dumps({"error": data["error"]}, ensure_ascii=False, indent=2)

    result = data.get("result", {})
    status = result.get("status", {})
    state = status.get("state", "")

    # Extract response text from artifacts
    response_text = ""
    for art in result.get("artifacts", []):
        for part in art.get("parts", []):
            if "text" in part:
                response_text += part["text"]

    # Extract status message
    status_message = ""
    status_msg = status.get("message", {})
    if status_msg:
        for part in status_msg.get("parts", []):
            if "text" in part:
                status_message += part["text"]

    output = {
        "task_id": result.get("id", task_id),
        "status": state,
        "response": response_text or None,
        "status_message": status_message or None,
    }
    if result.get("contextId"):
        output["context_id"] = result["contextId"]

    return json.dumps(output, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("a2a_discover", {
            "name": "a2a_discover",
            "description": (
                "Discover an A2A (Agent-to-Agent) agent by fetching its Agent Card. "
                "Returns the agent's name, description, capabilities, and available skills. "
                "Use this to learn what another agent can do before sending it a task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Base URL of the A2A agent (e.g. 'http://localhost:18800')",
                    },
                },
                "required": ["url"],
            },
        }, _a2a_discover),

        ToolEntry("a2a_send", {
            "name": "a2a_send",
            "description": (
                "Send a message to another A2A agent. Creates a task on the remote agent. "
                "Returns the task ID, status, and response (if the task completed immediately). "
                "For long-running tasks, use a2a_status to check progress later."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Base URL of the A2A agent",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to send to the agent",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional: ID of existing task to continue a dialogue",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Optional: context ID for grouping related tasks",
                    },
                },
                "required": ["url", "message"],
            },
        }, _a2a_send),

        ToolEntry("a2a_status", {
            "name": "a2a_status",
            "description": (
                "Check the status of a task on a remote A2A agent. "
                "Returns current state (working/completed/failed/etc), "
                "response text if completed, and any status messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Base URL of the A2A agent",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "The task ID returned by a2a_send",
                    },
                },
                "required": ["url", "task_id"],
            },
        }, _a2a_status),
    ]
