"""Durable actionable improvement backlog stored in the knowledge base."""

from __future__ import annotations

import hashlib
import pathlib
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from ouroboros.platform_layer import file_lock_exclusive, file_lock_shared, file_unlock
from ouroboros.utils import utc_now_iso

BACKLOG_TOPIC = "improvement-backlog"
BACKLOG_REL_PATH = f"memory/knowledge/{BACKLOG_TOPIC}.md"
_BACKLOG_TITLE = "# Improvement Backlog"
_BACKLOG_PREAMBLE = (
    "This topic stores concrete, evidence-backed improvement items discovered during task execution.\n"
    "Items here are advisory backlog nominations, not auto-started work.\n"
    "Before implementation, run plan_task for non-trivial backlog items."
)
_DEFAULT_BACKLOG_TEXT = f"{_BACKLOG_TITLE}\n\n{_BACKLOG_PREAMBLE}\n"


def backlog_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / BACKLOG_REL_PATH


@contextmanager
def _locked_text_file(path: pathlib.Path, mode: str, *, shared: bool = False) -> Iterator[Any]:
    fh = open(path, mode, encoding="utf-8")
    try:
        if shared:
            file_lock_shared(fh.fileno())
        else:
            file_lock_exclusive(fh.fileno())
        yield fh
    finally:
        try:
            file_unlock(fh.fileno())
        except Exception:
            pass
        fh.close()


def ensure_backlog_file(drive_root: Any) -> pathlib.Path:
    path = backlog_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_text_file(path, mode="a+") as fh:
        fh.seek(0)
        current = fh.read()
        if not current:
            fh.write(_DEFAULT_BACKLOG_TEXT)
            fh.flush()
    return path


def _stable_fingerprint(summary: str, category: str, source: str) -> str:
    key = " | ".join(
        re.sub(r"\s+", " ", str(value or "")).strip().lower()
        for value in (summary, category, source)
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _parse_backlog_items(text: str) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    current: Dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            if current:
                items.append(current)
            current = {"id": line[4:].strip()}
            continue
        if current is None:
            continue
        if line.startswith("- ") and ": " in line:
            key, value = line[2:].split(": ", 1)
            current[key.strip()] = value.strip()

    if current:
        items.append(current)
    return items


def load_backlog_items(drive_root: Any) -> List[Dict[str, str]]:
    path = backlog_path(drive_root)
    if not path.exists():
        return []
    with _locked_text_file(path, mode="r", shared=True) as fh:
        text = fh.read()
    return _parse_backlog_items(text)


def append_backlog_items(drive_root: Any, items: List[Dict[str, Any]]) -> int:
    if not items:
        return 0

    def _sanitize(value: Any, limit: int = 300) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"

    path = ensure_backlog_file(drive_root)
    with _locked_text_file(path, mode="r+") as fh:
        existing_text = fh.read()
        existing = _parse_backlog_items(existing_text)
        seen = {item.get("fingerprint", "") for item in existing}
        blocks: List[str] = []
        added = 0

        for item in items:
            summary = _sanitize(item.get("summary", ""), 260)
            if not summary:
                continue
            category = _sanitize(item.get("category", "process"), 60) or "process"
            source = _sanitize(item.get("source", "task"), 60) or "task"
            fingerprint = str(item.get("fingerprint") or _stable_fingerprint(summary, category, source))
            if fingerprint in seen:
                continue
            entry = {
                "id": str(item.get("id") or f"ibl-{fingerprint}"),
                "status": _sanitize(item.get("status", "open"), 40) or "open",
                "created_at": _sanitize(item.get("created_at", utc_now_iso()), 40),
                "source": source,
                "category": category,
                "task_id": _sanitize(item.get("task_id", ""), 80),
                "requires_plan_review": "yes" if item.get("requires_plan_review", True) else "no",
                "fingerprint": fingerprint,
                "summary": summary,
                "evidence": _sanitize(item.get("evidence", ""), 260),
                "context": _sanitize(item.get("context", ""), 400),
                "proposed_next_step": _sanitize(item.get("proposed_next_step", ""), 260),
            }
            block = [f"### {entry['id']}"]
            for key in (
                "status",
                "created_at",
                "source",
                "category",
                "task_id",
                "requires_plan_review",
                "fingerprint",
                "summary",
                "evidence",
                "context",
                "proposed_next_step",
            ):
                if entry[key]:
                    block.append(f"- {key}: {entry[key]}")
            blocks.append("\n".join(block))
            seen.add(fingerprint)
            added += 1

        if not blocks:
            return 0

        current = existing_text.rstrip() or _DEFAULT_BACKLOG_TEXT.rstrip()
        new_text = current + "\n\n" + "\n\n".join(blocks) + "\n"
        fh.seek(0)
        fh.write(new_text)
        fh.truncate()
        fh.flush()

    try:
        from ouroboros.consolidator import _rebuild_knowledge_index
        _rebuild_knowledge_index(path.parent)
    except Exception:
        pass
    return added


def format_backlog_digest(drive_root: Any, *, limit: int = 5, max_chars: int = 2500) -> str:
    items = [item for item in load_backlog_items(drive_root) if item.get("status", "open") == "open"]
    if not items:
        return ""

    items.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    visible = items[:limit]
    lines = [
        "## Improvement Backlog",
        "",
        f"- open_items: {len(items)}",
        "- policy: advisory backlog only; run plan_task before implementation",
    ]
    for item in visible:
        bits = [f"[{item.get('id', '?')}]", item.get("summary", "(missing summary)")]
        meta = []
        if item.get("category"):
            meta.append(f"category={item['category']}")
        if item.get("source"):
            meta.append(f"source={item['source']}")
        if item.get("task_id"):
            meta.append(f"task={item['task_id']}")
        line = "- " + " ".join(bits)
        if meta:
            line += " (" + ", ".join(meta) + ")"
        lines.append(line)
    omitted = len(items) - len(visible)
    if omitted > 0:
        lines.append(f"- ⚠️ OMISSION NOTE: {omitted} additional open backlog items not shown")

    text = "\n".join(lines)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n⚠️ OMISSION NOTE: backlog digest truncated at {max_chars} chars; original length {len(text)}"
    return text
