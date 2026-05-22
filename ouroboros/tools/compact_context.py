"""LLM-requested tool-history compaction trigger."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolEntry

log = logging.getLogger(__name__)


def _compact_context(ctx, keep_last_n: int = 6, **kwargs) -> str:
    """Store a pending compaction request for the next loop round."""

    keep_last_n = max(2, min(keep_last_n, 20))

    ctx._pending_compaction = keep_last_n

    return (
        f"✅ Context compaction scheduled: keeping last {keep_last_n} tool rounds intact, "
        f"older rounds will be summarized to 1-line summaries. "
        f"This will take effect on the next round."
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="compact_context",
            schema={
                "name": "compact_context",
                "description": (
                    "Selectively compress old tool results in conversation history to save context tokens. "
                    "Call this when you notice context is getting large (e.g., after self-check reminder). "
                    "Keeps recent N tool rounds intact; older rounds get summarized to 1-line summaries. "
                    "You decide what to keep (via keep_last_n) — no information is lost, just compressed."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keep_last_n": {
                            "type": "integer",
                            "description": "Number of recent tool rounds to keep fully intact (default 6, range 2-20). Lower = more compression.",
                            "default": 6,
                        },
                    },
                    "required": [],
                },
            },
            handler=_compact_context,
            timeout_sec=15,
        ),
    ]
