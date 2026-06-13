"""Observed LLM call helpers for non-loop decision surfaces."""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Tuple

from ouroboros.observability import new_call_id, persist_call
from ouroboros.utils import sanitize_tool_result_for_log


def _root(drive_root: Any) -> pathlib.Path:
    try:
        return pathlib.Path(drive_root)
    except TypeError:
        return pathlib.Path("../data")


def _base_manifest(call_type: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "call_type": call_type,
        "model": kwargs.get("model"),
        "reasoning_effort": kwargs.get("reasoning_effort"),
        "max_tokens": kwargs.get("max_tokens"),
        "use_local": kwargs.get("use_local"),
    }


def chat_observed(
    llm: Any,
    *,
    drive_root: Any,
    task_id: str = "",
    call_type: str = "llm_call",
    **kwargs: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run ``llm.chat`` while preserving request/response/error payloads."""

    root = _root(drive_root)
    call_id = new_call_id(call_type)
    try:
        persist_call(
            root,
            task_id=task_id or call_type,
            call_id=f"{call_id}_request",
            call_type=f"{call_type}_request",
            payload={"kwargs": kwargs},
            manifest=_base_manifest(call_type, kwargs),
        )
    except Exception:
        pass
    try:
        msg, usage = llm.chat(**kwargs)
    except Exception as exc:
        safe = sanitize_tool_result_for_log(f"{type(exc).__name__}: {exc}")
        try:
            persist_call(
                root,
                task_id=task_id or call_type,
                call_id=f"{call_id}_error",
                call_type=f"{call_type}_error",
                payload={"error": f"{type(exc).__name__}: {exc}", "kwargs": kwargs},
                manifest={**_base_manifest(call_type, kwargs), "status": "error", "error": safe},
            )
        except Exception:
            pass
        raise
    try:
        persist_call(
            root,
            task_id=task_id or call_type,
            call_id=f"{call_id}_response",
            call_type=f"{call_type}_response",
            payload={"message": msg, "usage": usage},
            manifest={**_base_manifest(call_type, kwargs), "status": "ok"},
        )
    except Exception:
        pass
    return msg, usage


async def chat_async_observed(
    llm: Any,
    *,
    drive_root: Any,
    task_id: str = "",
    call_type: str = "llm_call",
    **kwargs: Any,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Async variant for review surfaces using ``llm.chat_async``."""

    root = _root(drive_root)
    call_id = new_call_id(call_type)
    try:
        persist_call(
            root,
            task_id=task_id or call_type,
            call_id=f"{call_id}_request",
            call_type=f"{call_type}_request",
            payload={"kwargs": kwargs},
            manifest=_base_manifest(call_type, kwargs),
        )
    except Exception:
        pass
    try:
        msg, usage = await llm.chat_async(**kwargs)
    except Exception as exc:
        safe = sanitize_tool_result_for_log(f"{type(exc).__name__}: {exc}")
        try:
            persist_call(
                root,
                task_id=task_id or call_type,
                call_id=f"{call_id}_error",
                call_type=f"{call_type}_error",
                payload={"error": f"{type(exc).__name__}: {exc}", "kwargs": kwargs},
                manifest={**_base_manifest(call_type, kwargs), "status": "error", "error": safe},
            )
        except Exception:
            pass
        raise
    try:
        persist_call(
            root,
            task_id=task_id or call_type,
            call_id=f"{call_id}_response",
            call_type=f"{call_type}_response",
            payload={"message": msg, "usage": usage},
            manifest={**_base_manifest(call_type, kwargs), "status": "ok"},
        )
    except Exception:
        pass
    return msg, usage
