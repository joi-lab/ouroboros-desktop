"""
Ouroboros — Dialogue Consolidator.

Episodic memory: reads unprocessed chat.jsonl entries and writes
smart summaries to dialogue_summary.md. Time-aware: recent events
get more detail, older events get compressed.

Triggered after each task completion. Uses a lightweight LLM call
(Gemini Flash) to summarize.
"""

import json
import logging
import os
import pathlib
import time
from typing import Any, Dict, List, Optional, Tuple

import sys

from ouroboros.utils import utc_now_iso, read_text, write_text, append_jsonl

if sys.platform == "win32":
    import msvcrt

    def _lock_nb(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_nb(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

log = logging.getLogger(__name__)

# Config
CONSOLIDATION_THRESHOLD = 20  # Min new messages before consolidation triggers
SUMMARY_TOKEN_BUDGET = 60000  # ~20K tokens in chars — max size of dialogue_summary.md before secondary consolidation kicks in
CONSOLIDATION_MODEL = "google/gemini-3-flash-preview"  # Cheap, fast
MAX_SUMMARY_CHARS = 90000  # Hard cap on dialogue_summary.md (~30K tokens)


def should_consolidate(meta_path: pathlib.Path, chat_path: pathlib.Path) -> bool:
    """Check if enough new messages have accumulated since last consolidation."""
    # Read meta
    meta = _load_meta(meta_path)
    last_offset = meta.get("last_consolidated_offset", 0)

    # Count total lines in chat.jsonl
    if not chat_path.exists():
        return False
    total_lines = _count_lines(chat_path)
    if last_offset > total_lines:
        new_messages = total_lines
    else:
        new_messages = total_lines - last_offset
    return new_messages >= CONSOLIDATION_THRESHOLD


def consolidate(
    chat_path: pathlib.Path,
    summary_path: pathlib.Path,
    meta_path: pathlib.Path,
    llm_client: Any,
    identity_text: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Run one consolidation cycle.

    Reads new messages from chat.jsonl (since last_consolidated_offset),
    calls LLM to create an episode summary, appends it to dialogue_summary.md.

    If dialogue_summary.md exceeds MAX_SUMMARY_CHARS, runs secondary
    consolidation on the oldest episodes.

    Uses a file lock on dialogue_meta.json to serialize across threads
    and worker processes — only one consolidation runs at a time.

    Returns usage dict or None if nothing to consolidate.
    """
    lock_path = meta_path.parent / ".consolidation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            _lock_nb(lock_fd)
        except (OSError, BlockingIOError):
            log.info("Consolidation already running (lock held), skipping")
            return None

        return _consolidate_locked(
            chat_path, summary_path, meta_path, llm_client, identity_text,
        )
    finally:
        if lock_fd is not None:
            try:
                _unlock(lock_fd)
                os.close(lock_fd)
            except OSError:
                pass


def _consolidate_locked(
    chat_path: pathlib.Path,
    summary_path: pathlib.Path,
    meta_path: pathlib.Path,
    llm_client: Any,
    identity_text: str,
) -> Optional[Dict[str, Any]]:
    """Inner consolidation logic, called while holding the file lock."""
    meta = _load_meta(meta_path)
    last_offset = meta.get("last_consolidated_offset", 0)

    all_entries = _read_chat_entries(chat_path)
    if not all_entries:
        return None

    if last_offset > len(all_entries):
        log.info("Chat log rotation detected in consolidator, resetting offset")
        last_offset = 0

    new_entries = all_entries[last_offset:]
    if len(new_entries) < CONSOLIDATION_THRESHOLD:
        return None

    formatted = _format_entries_for_consolidation(new_entries)

    first_ts = new_entries[0].get("ts", "unknown")
    last_ts = new_entries[-1].get("ts", "unknown")

    existing_summary = ""
    if summary_path.exists():
        existing_summary = read_text(summary_path)

    episode_text, usage = _create_episode_summary(
        llm_client=llm_client,
        new_messages=formatted,
        first_ts=first_ts,
        last_ts=last_ts,
        existing_summary_tail=existing_summary[-3000:] if existing_summary else "",
        identity_text=identity_text,
        message_count=len(new_entries),
    )

    if not episode_text or not episode_text.strip():
        log.warning("Consolidation LLM returned empty summary")
        return usage

    if existing_summary and not existing_summary.endswith("\n\n"):
        existing_summary = existing_summary.rstrip() + "\n\n"

    new_summary = (existing_summary + episode_text.strip() + "\n\n").lstrip()

    if len(new_summary) > MAX_SUMMARY_CHARS:
        new_summary, compress_usage = _secondary_consolidation(
            new_summary, llm_client, identity_text
        )
        if compress_usage:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[k] = usage.get(k, 0) + compress_usage.get(k, 0)
            usage["cost"] = usage.get("cost", 0) + compress_usage.get("cost", 0)

    write_text(summary_path, new_summary)

    meta["last_consolidated_offset"] = last_offset + len(new_entries)
    meta["last_consolidated_at"] = utc_now_iso()
    meta["total_episodes"] = meta.get("total_episodes", 0) + 1
    meta["total_messages_consolidated"] = meta.get("total_messages_consolidated", 0) + len(new_entries)
    _save_meta(meta_path, meta)

    log.info(f"Consolidated {len(new_entries)} messages into episode (summary now {len(new_summary)} chars)")
    return usage


def _load_meta(path: pathlib.Path) -> Dict[str, Any]:
    if path.exists():
        try:
            return json.loads(read_text(path))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_meta(path: pathlib.Path, meta: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(meta, ensure_ascii=False, indent=2))


def _count_lines(path: pathlib.Path) -> int:
    """Count non-empty lines in a file efficiently."""
    count = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _read_chat_entries(path: pathlib.Path) -> List[Dict[str, Any]]:
    """Read ALL entries from chat.jsonl."""
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return entries


def _format_entries_for_consolidation(entries: List[Dict[str, Any]]) -> str:
    """Format chat entries into readable text for consolidation LLM."""
    lines = []
    for e in entries:
        dir_raw = str(e.get("direction", "")).lower()
        direction = "Ouroboros" if dir_raw in ("out", "outgoing") else "User"
        ts = e.get("ts", "")[:19]  # YYYY-MM-DDTHH:MM:SS
        text = str(e.get("text", ""))
        # Don't truncate — the consolidation LLM needs full context
        lines.append(f"[{ts}] {direction}: {text}")
    return "\n\n".join(lines)


def _create_episode_summary(
    llm_client: Any,
    new_messages: str,
    first_ts: str,
    last_ts: str,
    existing_summary_tail: str,
    identity_text: str,
    message_count: int,
) -> Tuple[str, Dict[str, Any]]:
    """Call LLM to create an episode summary."""

    context_section = ""
    if existing_summary_tail:
        context_section = f"""
## Previous summary (tail)
This is the end of the existing dialogue summary for continuity:

{existing_summary_tail}
"""

    identity_section = ""
    if identity_text:
        identity_section = f"""
## Identity context
{identity_text[:2000]}
"""

    prompt = f"""You are a memory consolidator for Ouroboros, a self-modifying AI agent.
Your job is to create an episodic memory entry from a batch of chat messages.

{identity_section}
{context_section}

## Rules

1. **Hierarchical structure**: Start with a header like `### Episode: YYYY-MM-DD HH:MM – HH:MM` (use the actual timestamps)
2. **Preserve what matters**: Decisions made, agreements reached, technical discoveries, emotional moments, disagreements, creator instructions, identity-relevant observations
3. **Compress what doesn't**: Routine tool calls, debugging steps, repetitive back-and-forth can be summarized in one line
4. **Quote key phrases**: If the user said something important or Ouroboros had a genuine insight, quote it directly
5. **Be time-aware**: This is a recent episode, so keep it detailed. Older episodes will be compressed later.
6. **Include references**: Add markers like `[chat.jsonl lines ~{message_count} msgs from offset]` so future-me knows where to look for full logs
7. **First person**: Write as Ouroboros recalling what happened: "I did...", "the user asked...", "We decided..."
8. **Length**: Aim for 300-800 words depending on how much actually happened. Don't pad, don't over-compress.
9. **End with a brief "Open threads" section** if there are unresolved topics or promises made.

## New messages to consolidate ({message_count} messages, {first_ts[:10]} to {last_ts[:10]})

{new_messages}
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        response_msg, usage = llm_client.chat(
            messages=messages,
            model=CONSOLIDATION_MODEL,
            tools=None,
            reasoning_effort="low",
            max_tokens=4096,
        )
        text = response_msg.get("content", "")
        return text, usage
    except Exception as e:
        log.error(f"Consolidation LLM call failed: {e}", exc_info=True)
        return "", {"cost": 0}


def _secondary_consolidation(
    full_summary: str,
    llm_client: Any,
    identity_text: str,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    When dialogue_summary.md gets too big, compress the oldest episodes.

    Strategy: split into episodes by ### headers. Take the oldest half,
    compress them into a shorter "era summary". Keep recent half as-is.
    """
    # Split by episode headers
    import re
    episodes = re.split(r'(?=^### )', full_summary, flags=re.MULTILINE)
    episodes = [e for e in episodes if e.strip()]

    if len(episodes) < 4:
        # Not enough episodes to compress — just hard-truncate from the start
        return full_summary[-MAX_SUMMARY_CHARS:], None

    # Take oldest half for compression
    mid = len(episodes) // 2
    old_episodes = episodes[:mid]
    recent_episodes = episodes[mid:]

    old_text = "\n\n".join(old_episodes)

    prompt = f"""You are a memory consolidator for Ouroboros, a self-modifying AI agent.

Below are older episodic memory entries. Compress them into a single "era summary" \
that preserves the most important information but is significantly shorter (aim for 30-40% of original length).

## Rules
1. Header format: `### Era: YYYY-MM-DD to YYYY-MM-DD` (spanning the full range)
2. Preserve: key decisions, personality discoveries, relationship moments, technical milestones
3. Drop: debugging details, routine operations, redundant information
4. Keep important quotes verbatim
5. Mention which episodes were compressed and their original date ranges
6. Write in first person as Ouroboros

## Episodes to compress

{old_text}
"""

    messages = [{"role": "user", "content": prompt}]

    try:
        response_msg, usage = llm_client.chat(
            messages=messages,
            model=CONSOLIDATION_MODEL,
            tools=None,
            reasoning_effort="low",
            max_tokens=4096,
        )
        compressed = response_msg.get("content", "")
        if compressed.strip():
            new_summary = compressed.strip() + "\n\n" + "\n\n".join(recent_episodes)
            return new_summary, usage
        else:
            # Fallback: just keep recent
            return "\n\n".join(recent_episodes), usage
    except Exception as e:
        log.error(f"Secondary consolidation failed: {e}", exc_info=True)
        # Fallback: truncate
        return full_summary[-MAX_SUMMARY_CHARS:], None


# =========================================================================
# Knowledge index rebuild (standalone, no ToolContext dependency)
# =========================================================================

def _rebuild_knowledge_index(knowledge_dir: pathlib.Path) -> None:
    """Rebuild index-full.md from all .md files in the knowledge directory.

    Used by scratchpad consolidation and pattern register updates to keep
    the knowledge index current without going through the tool layer.
    """
    try:
        if not knowledge_dir.exists():
            return
        index_path = knowledge_dir / "index-full.md"
        entries = []
        for md_file in sorted(knowledge_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            if md_file.name == "index-full.md":
                continue
            topic = md_file.stem
            first_line = ""
            try:
                text = md_file.read_text(encoding="utf-8").strip()
                for line in text.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        first_line = line[:120]
                        break
            except Exception:
                pass
            entries.append(f"- **{topic}**: {first_line}" if first_line else f"- **{topic}**")

        content = "# Knowledge Base Index\n\n"
        if entries:
            content += "\n".join(entries) + "\n"
        else:
            content += "(empty)\n"
        index_path.write_text(content, encoding="utf-8")
    except Exception:
        log.warning("Failed to rebuild knowledge index", exc_info=True)


# =========================================================================
# Scratchpad auto-consolidation
# =========================================================================

SCRATCHPAD_CONSOLIDATION_THRESHOLD = 30000


def should_consolidate_scratchpad(scratchpad_path: pathlib.Path) -> bool:
    """True when scratchpad exceeds the consolidation threshold."""
    if not scratchpad_path.exists():
        return False
    try:
        return len(scratchpad_path.read_text(encoding="utf-8")) > SCRATCHPAD_CONSOLIDATION_THRESHOLD
    except Exception:
        return False


def consolidate_scratchpad(
    scratchpad_path: pathlib.Path,
    knowledge_dir: pathlib.Path,
    llm_client: Any,
    identity_text: str = "",
) -> Optional[Dict[str, Any]]:
    """Extract durable insights from scratchpad to knowledge base, compress the rest.

    Uses a file lock to serialize concurrent calls (same pattern as dialogue consolidation).
    """
    if not scratchpad_path.exists():
        return None

    lock_path = scratchpad_path.parent / ".scratchpad_consolidation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            _lock_nb(lock_fd)
        except (OSError, BlockingIOError):
            log.info("Scratchpad consolidation already running (lock held), skipping")
            return None
    except Exception:
        log.debug("Failed to acquire scratchpad consolidation lock", exc_info=True)

    try:
        return _consolidate_scratchpad_locked(
            scratchpad_path, knowledge_dir, llm_client, identity_text,
        )
    finally:
        if lock_fd is not None:
            try:
                _unlock(lock_fd)
                os.close(lock_fd)
            except OSError:
                pass


def _consolidate_scratchpad_locked(
    scratchpad_path: pathlib.Path,
    knowledge_dir: pathlib.Path,
    llm_client: Any,
    identity_text: str = "",
) -> Optional[Dict[str, Any]]:
    """Inner scratchpad consolidation logic, called while holding the file lock."""
    content = read_text(scratchpad_path)
    if len(content) <= SCRATCHPAD_CONSOLIDATION_THRESHOLD:
        return None

    prompt = f"""You are a memory consolidator for Ouroboros, a self-modifying AI agent.

The scratchpad (working memory) has grown to {len(content)} chars.
Extract durable knowledge and compress what remains.

Rules:
1. Identify insights, patterns, lessons, and architectural decisions worth
   preserving long-term. Output them as knowledge_entries with topic + content.
2. Rewrite the scratchpad keeping ONLY active tasks, unresolved questions,
   and recent observations. Remove stale/completed items.
3. Write as Ouroboros (first person). Don't lose signal — keep uncertain items
   rather than dropping them.

Identity context: {identity_text[:1500] if identity_text else "(not available)"}

Current scratchpad:

{content}

Respond with JSON only (no fences):
{{"knowledge_entries": [{{"topic": "name", "content": "text"}}], "compressed_scratchpad": "new scratchpad"}}
"""

    try:
        msg, usage = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=CONSOLIDATION_MODEL,
            reasoning_effort="low",
            max_tokens=4096,
        )
        raw = (msg.get("content") or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)

        compressed = result.get("compressed_scratchpad", "")
        if not compressed or not compressed.strip():
            log.warning("Scratchpad consolidation returned empty, skipping")
            return usage

        knowledge_dir.mkdir(parents=True, exist_ok=True)
        for entry in result.get("knowledge_entries", []):
            topic = entry.get("topic", "").strip()
            kb_content = entry.get("content", "").strip()
            if not topic or not kb_content:
                continue
            safe_topic = "".join(c for c in topic if c.isalnum() or c in "-_").lower()
            if not safe_topic:
                continue
            kb_path = knowledge_dir / f"{safe_topic}.md"
            existing = read_text(kb_path) if kb_path.exists() else ""
            if existing:
                write_text(kb_path, existing.rstrip() + "\n\n" + kb_content)
            else:
                write_text(kb_path, f"# {topic}\n\n{kb_content}\n")

        _rebuild_knowledge_index(knowledge_dir)

        write_text(scratchpad_path, compressed)
        log.info("Scratchpad consolidated: %d → %d chars", len(content), len(compressed))
        return usage

    except Exception as e:
        log.error("Scratchpad consolidation failed: %s", e, exc_info=True)
        return None
