"""Tool-history compaction with safe structural fallback on LLM failure."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_COMPACTION_PROTECTED_TOOLS = frozenset({
    "repo_commit",
    "knowledge_read",
    "data_read",
})

_SUMMARY_INPUT_LIMIT = 2500
_BLOCKS_PER_BATCH = 8


def _find_tool_name_for_result(msg: dict, messages: list) -> str:
    """Look up which tool produced a given tool-result message."""
    target_id = msg.get("tool_call_id", "")
    if not target_id:
        return ""
    msg_idx = None
    for idx, item in enumerate(messages):
        if item is msg:
            msg_idx = idx
            break
    if msg_idx is None:
        return ""
    for j in range(msg_idx - 1, -1, -1):
        prev = messages[j]
        if prev.get("role") != "assistant":
            continue
        for tc in (prev.get("tool_calls") or []):
            if tc.get("id") == target_id:
                return tc.get("function", {}).get("name", "")
        break
    return ""


def _tool_round_starts(messages: list) -> list[int]:
    return [
        idx for idx, msg in enumerate(messages)
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]


def _tool_round_spans(messages: list) -> list[Tuple[int, int]]:
    starts = _tool_round_starts(messages)
    spans: list[Tuple[int, int]] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] - 1 if idx + 1 < len(starts) else len(messages) - 1
        spans.append((start, end))
    return spans


def _round_has_protected_content(messages: list, start: int, end: int) -> bool:
    for idx in range(start, end + 1):
        msg = messages[idx]
        role = msg.get("role", "")
        content = str(msg.get("content") or "")
        # Protect critical tool results and error markers from compaction.
        if role == "tool":
            tool_name = _find_tool_name_for_result(msg, messages)
            if tool_name in _COMPACTION_PROTECTED_TOOLS or content.startswith("⚠️"):
                return True
    return False


def _excerpt_for_summary(text: str, limit: int = _SUMMARY_INPUT_LIMIT) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n<<EXCERPT_FOR_COMPACTION len={len(text)} chars>>"


def _compact_argument_value(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return {"_depth_limit": True, "_type": type(value).__name__}
    if isinstance(value, str):
        if len(value) <= 160:
            return value
        return f"<<LONG_STRING len={len(value)}>>"
    if isinstance(value, dict):
        return {k: _compact_argument_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > 20:
            return {"_list_len": len(value), "_type": "list"}
        return [_compact_argument_value(v, depth + 1) for v in value]
    return value


def _compact_tool_call_arguments(tool_name: str, args_json: str) -> Dict[str, Any]:
    """Compact tool call arguments for old rounds without silent truncation."""
    large_content_tools = {
        "repo_write": "content",
        "data_write": "content",
        "claude_code_edit": "prompt",
        "update_scratchpad": "content",
        "update_identity": "content",
    }

    try:
        args = json.loads(args_json)
        if not isinstance(args, dict):
            return {"name": tool_name, "arguments": f"<<NON_DICT_ARGS type={type(args).__name__}>>"}

        compacted = dict(args)
        large_field = large_content_tools.get(tool_name)
        if large_field and large_field in compacted and compacted[large_field]:
            raw = compacted[large_field]
            raw_str = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            compacted[large_field] = f"<<CONTENT_OMITTED len={len(raw_str)}>>"

        compacted = {k: _compact_argument_value(v) for k, v in compacted.items()}
        return {"name": tool_name, "arguments": json.dumps(compacted, ensure_ascii=False)}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {"name": tool_name, "arguments": f"<<UNPARSEABLE_ARGS_JSON len={len(args_json)}>>"}


def _render_round_block(messages: list, start: int, end: int) -> str:
    lines: list[str] = []
    for idx in range(start, end + 1):
        msg = messages[idx]
        role = msg.get("role")
        if role == "assistant":
            content = str(msg.get("content") or "").strip()
            if content:
                lines.append("REASONING:")
                lines.append(_excerpt_for_summary(content))
            for tc in msg.get("tool_calls") or []:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                args_json = func.get("arguments", "")
                compacted = _compact_tool_call_arguments(tool_name, args_json) if args_json else {"name": tool_name, "arguments": "{}"}
                lines.append(f"TOOL_CALL {compacted['name']}: {compacted['arguments']}")
        elif role == "tool":
            tool_name = _find_tool_name_for_result(msg, messages) or "unknown_tool"
            content = str(msg.get("content") or "")
            lines.append(f"TOOL_RESULT {tool_name}:")
            lines.append(_excerpt_for_summary(content))
        elif role == "user":
            content = str(msg.get("content") or "")
            lines.append("USER_INPUT:")
            lines.append(_excerpt_for_summary(content))
    return "\n".join(lines).strip()


def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """Safe fallback: preserve full content, compact only oversized tool-call payloads."""
    spans = _tool_round_spans(messages)
    if len(spans) <= keep_recent:
        return messages

    compactable_starts = {start for start, _ in spans[:-keep_recent]}
    result = []
    for idx, msg in enumerate(messages):
        if idx in compactable_starts and msg.get("role") == "assistant" and msg.get("tool_calls"):
            compacted = dict(msg)
            compacted_calls = []
            for tc in msg.get("tool_calls") or []:
                tc_copy = dict(tc)
                if "function" in tc_copy:
                    func = dict(tc_copy["function"])
                    args_str = func.get("arguments", "")
                    tc_copy["function"] = _compact_tool_call_arguments(func.get("name", ""), args_str) if args_str else func
                compacted_calls.append(tc_copy)
            compacted["tool_calls"] = compacted_calls
            result.append(compacted)
            continue
        result.append(msg)
    return result


def _summarize_round_batch(
    rendered_blocks: List[Tuple[int, str]],
) -> Tuple[Dict[int, str], Dict[str, Any]]:
    batch_text = "\n\n---\n\n".join(
        f"[round:{start}]\n{content}" for start, content in rendered_blocks
    )
    prompt = (
        "Summarize each reasoning round block below. Preserve: user steering, "
        "key hypotheses, tools used only when relevant, outcomes, what changed, "
        "and the next step or open question. Write as Ouroboros in first person. "
        "Keep each summary to 3-6 sentences. Output one block per [round:id] in the same order.\n\n"
        + batch_text
    )

    from ouroboros.config import get_light_model
    from ouroboros.llm import LLMClient

    light_model = get_light_model()
    client = LLMClient()
    use_local_light = os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1")
    resp_msg, usage = client.chat(
        messages=[{"role": "user", "content": prompt}],
        model=light_model,
        reasoning_effort="low",
        max_tokens=32768,
        use_local=use_local_light,
    )
    summary_text = resp_msg.get("content") or ""
    if not summary_text.strip():
        raise ValueError("empty summary response")

    summary_map: Dict[int, str] = {}
    current_round: Optional[int] = None
    current_lines: list[str] = []
    for line in summary_text.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("[round:") and stripped.endswith("]"):
            if current_round is not None:
                summary_map[current_round] = " ".join(current_lines).strip()
            current_lines = []
            try:
                current_round = int(stripped[len("[round:"):-1])
            except ValueError:
                current_round = None
            continue
        if current_round is not None:
            current_lines.append(stripped)
    if current_round is not None:
        summary_map[current_round] = " ".join(current_lines).strip()

    return summary_map, usage


def compact_tool_history_llm(
    messages: list, keep_recent: int = 6,
) -> Tuple[list, Optional[Dict[str, Any]]]:
    """LLM-driven compaction of old reasoning rounds, with safe non-destructive fallback."""
    spans = _tool_round_spans(messages)
    if len(spans) <= keep_recent:
        return messages, None

    spans_to_keep = spans[-keep_recent:]
    keep_starts = {start for start, _ in spans_to_keep}
    compactable_spans = []
    protected_starts = set()
    for start, end in spans[:-keep_recent]:
        if _round_has_protected_content(messages, start, end):
            protected_starts.add(start)
            continue
        compactable_spans.append((start, end))

    if not compactable_spans:
        return messages, None

    rendered_blocks = [(start, _render_round_block(messages, start, end)) for start, end in compactable_spans]

    total_usage: Optional[Dict[str, Any]] = None
    summary_map: Dict[int, str] = {}
    try:
        for idx in range(0, len(rendered_blocks), _BLOCKS_PER_BATCH):
            batch = rendered_blocks[idx:idx + _BLOCKS_PER_BATCH]
            batch_map, usage = _summarize_round_batch(batch)
            for start, _ in batch:
                if not batch_map.get(start):
                    raise ValueError(f"missing compaction summary for round {start}")
            summary_map.update(batch_map)
            if total_usage is None:
                total_usage = dict(usage or {})
            elif usage:
                for key, value in usage.items():
                    if isinstance(value, (int, float)):
                        total_usage[key] = total_usage.get(key, 0) + value
                    else:
                        total_usage[key] = value
    except Exception:
        log.warning("LLM compaction failed, preserving original rounds", exc_info=True)
        return compact_tool_history(messages, keep_recent=keep_recent), None

    compacted_by_start = {start: (end, summary_map[start]) for start, end in compactable_spans}

    result = []
    idx = 0
    while idx < len(messages):
        block = compacted_by_start.get(idx)
        if block:
            end, summary = block
            result.append({
                "role": "assistant",
                "content": f"[Compacted reasoning block]\n{summary}",
            })
            idx = end + 1
            continue
        result.append(messages[idx])
        idx += 1

    return result, total_usage
