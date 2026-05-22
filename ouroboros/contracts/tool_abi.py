"""Frozen tool-module ABI: ToolEntry shape and get_tools() signature."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class ToolEntryProtocol(Protocol):
    """Structural contract for the public ToolEntry descriptor fields."""

    name: str
    schema: Dict[str, Any]
    handler: Callable[..., str]
    is_code_tool: bool
    timeout_sec: int


class GetToolsProtocol(Protocol):
    """Callable contract for ``get_tools()`` exported by every tools module."""

    def __call__(self) -> List[ToolEntryProtocol]: ...


__all__ = ["ToolEntryProtocol", "GetToolsProtocol"]
