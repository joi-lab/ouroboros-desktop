import json
import logging
import os
import pathlib
import hashlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso, read_text, write_text

from ouroboros.platform_layer import (
    file_lock_exclusive as _lock_ex,
    file_lock_exclusive_nb as _lock_nb,
    file_unlock as _unlock,
)

log = logging.getLogger(__name__)

BLOCK_SIZE = 100                          # Messages per consolidation block
MAX_SUMMARY_BLOCKS = 10                   # Compress into era when exceeded
ERA_COMPRESS_COUNT = 4                    # Oldest blocks to compress per era
CONSOLIDATION_MODEL = "google/gemini-3-flash-preview"
CONSOLIDATION_REASONING_EFFORT = "medium"

def should_consolidate(
    meta_path: pathlib.Path,
    chat_path: pathlib.Path,
) -> bool:
    if not chat_path.exists():
        return False
    meta = _load_meta(meta_path)
    last_offset = meta.get("last_consolidated_offset", 0)
    total = _count_lines(chat_path)
    if last_offset > total:
        return total >= BLOCK_SIZE
    return (total - last_offset) >= BLOCK_SIZE


def consolidate(
    chat_path: pathlib.Path,
    blocks_path: pathlib.Path,
    meta_path: pathlib.Path,
    llm_client: Any,
    identity_text: str = "",
) -> Optional[Dict[str, Any]]:
    lock_path = meta_path.parent / ".consolidation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            _lock_nb(lock_fd)
        except (OSError, BlockingIOError):
            log.info("Chat block consolidation already running, skipping")
            return None

        return _run_block_consolidation(
            source_path=chat_path,
            blocks_path=blocks_path,
            meta_path=meta_path,
            llm_client=llm_client,
            identity_text=identity_text,
        )
    finally:
        if lock_fd is not None:
            try:
                _unlock(lock_fd)
                os.close(lock_fd)
            except OSError:
                pass
def _run_block_consolidation(
    source_path: pathlib.Path,
    blocks_path: pathlib.Path,
    meta_path: pathlib.Path,
    llm_client: Any,
    identity_text: str,
) -> Optional[Dict[str, Any]]:
    meta = _load_meta(meta_path)
    last_offset = meta.get("last_consolidated_offset", 0)

    all_entries = _read_chat_entries(source_path)
    if last_offset > len(all_entries):
        log.info("Chat log rotation detected, resetting offset")
        last_offset = 0

    new_entries = all_entries[last_offset:]
    if len(new_entries) < BLOCK_SIZE:
        return None

    total_usage: Dict[str, Any] = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0,
    }
    new_blocks: List[Dict[str, Any]] = []
    chunks_to_process = len(new_entries) // BLOCK_SIZE
    processed = 0

    for i in range(chunks_to_process):
        chunk = new_entries[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]
        formatted = _format_entries_for_block(chunk)
        first_ts = str(chunk[0].get("ts", "unknown"))
        last_ts = str(chunk[-1].get("ts", "unknown"))

        content, usage = _create_block_summary(
            llm_client=llm_client,
            messages_text=formatted,
            first_ts=first_ts,
            last_ts=last_ts,
            identity_text=identity_text,
            message_count=len(chunk),
        )

        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total_usage[key] += usage.get(key, 0)
        total_usage["cost"] += usage.get("cost", 0)

        if content and content.strip():
            first_date, last_date = first_ts[:10], last_ts[:10]
            first_time, last_time = first_ts[11:16], last_ts[11:16]
            if first_date == last_date:
                range_str = f"{first_date} {first_time} - {last_time}"
            else:
                range_str = f"{first_date} {first_time} - {last_date} {last_time}"

            new_blocks.append({
                "ts": utc_now_iso(),
                "type": "summary",
                "range": range_str,
                "message_count": len(chunk),
                "content": content.strip(),
            })
            processed += len(chunk)
        else:
            log.warning("Block summary empty for chunk %d, will retry next cycle", i)
            break

    if not new_blocks:
        meta["last_consolidated_offset"] = last_offset + processed
        meta["chat_log_signature"] = _chat_log_signature(source_path)
        _save_meta(meta_path, meta)
        return total_usage if total_usage["cost"] > 0 else None

    existing_blocks = _load_blocks(blocks_path)
    all_blocks = existing_blocks + new_blocks

    if len(all_blocks) > MAX_SUMMARY_BLOCKS:
        compress_count = min(ERA_COMPRESS_COUNT, len(all_blocks) - 1)
        old_blocks = all_blocks[:compress_count]
        remaining = all_blocks[compress_count:]
        era, era_usage = _compress_blocks_to_era(old_blocks, llm_client, identity_text)
        if era is not None:
            all_blocks = [era] + remaining
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total_usage[key] += era_usage.get(key, 0)
            total_usage["cost"] += era_usage.get("cost", 0)

    _save_blocks(blocks_path, all_blocks)

    meta["last_consolidated_offset"] = last_offset + processed
    meta["last_consolidated_at"] = utc_now_iso()
    meta["chat_log_signature"] = _chat_log_signature(source_path)
    _save_meta(meta_path, meta)

    log.info("Block consolidation: %d messages -> %d new blocks (total %d)",
             processed, len(new_blocks), len(all_blocks))
    return total_usage


def _call_consolidation_llm(llm_client: Any, prompt: str, label: str) -> Tuple[str, Dict[str, Any]]:
    try:
        msg, usage = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=CONSOLIDATION_MODEL,
            tools=None,
            reasoning_effort="low",
            max_tokens=16384,
        )
        return msg.get("content", ""), usage
    except Exception as e:
        log.error("%s failed: %s", label, e, exc_info=True)
        return "", {"cost": 0}


def _create_block_summary(
    llm_client: Any,
    messages_text: str,
    first_ts: str,
    last_ts: str,
    identity_text: str,
    message_count: int,
) -> Tuple[str, Dict[str, Any]]:
    first_date = first_ts[:10]
    first_time = first_ts[11:16]
    last_time = last_ts[11:16]

    identity_section = ""
    if identity_text:
        identity_section = f"\n## Identity context\n{identity_text}\n"

    prompt = f"""You are a memory consolidator for Ouroboros, a self-modifying AI agent.
Create a detailed episodic memory entry from these {message_count} messages.

## Rules
1. Header: ### Block: {first_date} {first_time} - {last_time}
2. Preserve: decisions, agreements, technical discoveries, emotional moments, task outcomes, what worked/failed
3. Compress: routine tool calls, repetitive back-and-forth
4. Quote key phrases directly when important
5. First person as Ouroboros: "I did...", "the user asked..."
6. Length: 200-500 words depending on content density
7. Include task_ids when referencing specific tasks
{identity_section}
## Messages to summarize
{messages_text}
"""

    return _call_consolidation_llm(llm_client, prompt, "Block summary LLM call")


def _compress_blocks_to_era(
    blocks: List[Dict[str, Any]],
    llm_client: Any,
    identity_text: str,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    start_date = blocks[0].get("range", "unknown")[:10]
    last_range = blocks[-1].get("range", "unknown")
    if " to " in last_range:
        end_date = last_range.split(" to ")[-1].strip()[:10]
    else:
        end_date = last_range[:10]

    combined = "\n\n---\n\n".join(
        f"### {b.get('range', 'unknown')}\n{b.get('content', '')}"
        for b in blocks
    )

    prompt = f"""Compress these older memory blocks into a single era summary.
Preserve: key decisions, personality discoveries, relationship moments, technical milestones.
Drop: debugging details, routine operations, redundant info.
Header: ### Era: {start_date} to {end_date}
Write as Ouroboros (first person). Aim for 30-40% of original length.

## Blocks to compress

{combined}
"""

    content, usage = _call_consolidation_llm(llm_client, prompt, "Era compression")
    if not content or not content.strip():
        log.warning("Era compression returned empty — keeping original blocks (Bible P1)")
        return None, usage
    era = {
        "ts": utc_now_iso(),
        "type": "era",
        "range": f"{start_date} to {end_date}",
        "message_count": sum(b.get("message_count", 0) for b in blocks),
        "content": content.strip(),
    }
    return era, usage

def _format_entries_for_block(entries: List[Dict[str, Any]]) -> str:
    lines = []
    for e in entries:
        ts_raw = str(e.get("ts", ""))
        ts = ts_raw[:10] + " " + ts_raw[11:16] if len(ts_raw) >= 16 else ts_raw
        dir_raw = str(e.get("direction", "")).lower()
        if dir_raw in ("out", "outgoing"):
            direction_prefix = "-> "
            author = "Ouroboros"
        elif dir_raw == "system":
            direction_prefix = "[system] "
            author = "Ouroboros"
        else:
            direction_prefix = ""
            author = e.get("username") or e.get("author") or "User"
        text = str(e.get("text", ""))
        lines.append(f"[{ts}] {direction_prefix}{author}: {text}")
    return "\n\n".join(lines)


def _load_blocks(path: pathlib.Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(read_text(path))
    except (json.JSONDecodeError, ValueError):
        log.warning("Corrupt blocks file %s, starting fresh", path)
        return []


def _save_blocks(path: pathlib.Path, blocks: List[Dict[str, Any]]) -> None:
    _write_locked_json(path, blocks)


def _write_locked_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        _lock_ex(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    finally:
        if fd is not None:
            try:
                _unlock(fd)
                os.close(fd)
            except OSError:
                pass

def _load_meta(path: pathlib.Path) -> Dict[str, Any]:
    return read_json_dict(path) or {}


def _save_meta(path: pathlib.Path, meta: Dict[str, Any]) -> None:
    atomic_write_json(path, meta)


def _chat_log_signature(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        stat = path.stat()
        with path.open("r", encoding="utf-8") as handle:
            first = next((line.strip() for line in handle if line.strip()), "")
        return {
            "first_line_sha256": hashlib.sha256(first.encode("utf-8", errors="replace")).hexdigest(),
            "size": int(stat.st_size),
        }
    except OSError:
        return {}


def _count_lines(path: pathlib.Path) -> int:
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _read_chat_entries(path: pathlib.Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not is_a2a_chat_id(entry.get("chat_id", 1)):
                entries.append(entry)
    return entries

def _rebuild_knowledge_index(knowledge_dir: pathlib.Path) -> None:
    try:
        if not knowledge_dir.exists():
            return
        entries = []
        for md_file in sorted(knowledge_dir.glob("*.md")):
            if md_file.name.startswith("_") or md_file.name == "index-full.md":
                continue
            topic = md_file.stem
            first_line = ""
            try:
                first_line = next(
                    (line.strip()[:120] for line in md_file.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.strip().startswith("#")),
                    "",
                )
            except Exception:
                pass
            entries.append(f"- **{topic}**: {first_line}" if first_line else f"- **{topic}**")
        write_text(knowledge_dir / "index-full.md", "# Knowledge Base Index\n\n" + "\n".join(entries) + "\n")
    except Exception:
        log.warning("Failed to rebuild knowledge index", exc_info=True)

SCRATCHPAD_CONSOLIDATION_THRESHOLD = 30000


def should_consolidate_scratchpad(memory: Any) -> bool:
    try:
        blocks = memory.load_scratchpad_blocks()
        return len(blocks) >= 3 and sum(len(b.get("content", "")) for b in blocks) > SCRATCHPAD_CONSOLIDATION_THRESHOLD
    except Exception:
        return False


def consolidate_scratchpad(
    memory: Any,
    knowledge_dir: pathlib.Path,
    llm_client: Any,
    identity_text: str = "",
) -> Optional[Dict[str, Any]]:
    blocks = memory.load_scratchpad_blocks()

    if len(blocks) < 3:
        return None
    return _consolidate_scratchpad_blocks(memory, blocks, knowledge_dir, llm_client, identity_text)


def _consolidate_scratchpad_blocks(
    memory: Any,
    blocks: List[Dict[str, Any]],
    knowledge_dir: pathlib.Path,
    llm_client: Any,
    identity_text: str,
) -> Optional[Dict[str, Any]]:
    total_chars = sum(len(b.get("content", "")) for b in blocks)
    if total_chars <= SCRATCHPAD_CONSOLIDATION_THRESHOLD:
        return None

    compress_count = max(2, len(blocks) // 2)
    old_blocks = blocks[:compress_count]
    recent_blocks = blocks[compress_count:]

    old_content = "\n\n---\n\n".join(
        f"[{b.get('ts', '?')[:16]} \u2014 {b.get('source', '?')}]\n{b.get('content', '')}"
        for b in old_blocks
    )

    prompt = f"""You are a memory consolidator for Ouroboros, a self-modifying AI agent.

The scratchpad working memory has {len(blocks)} blocks totaling {total_chars} chars.
The oldest {compress_count} blocks need compression.

Rules:
1. Identify insights, patterns, lessons, and architectural decisions worth
   preserving long-term. Output them as knowledge_entries with topic + content.
2. Compress the old blocks into a SINGLE shorter summary block. Keep active
   tasks, unresolved questions, admin instructions still in force. Remove
   stale/completed items and routine status updates.
3. Write as Ouroboros (first person). Don't lose signal — keep uncertain items
   rather than dropping them.

Identity context: {identity_text if identity_text else "(not available)"}

## Old blocks to compress

{old_content}

Respond with JSON only (no fences):
{{"knowledge_entries": [{{"topic": "name", "content": "text"}}], "compressed_block": "single compressed block text"}}
"""

    try:
        msg, usage = llm_client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=CONSOLIDATION_MODEL,
            reasoning_effort="low",
            max_tokens=16384,
        )
        raw = (msg.get("content") or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(raw)

        compressed_text = result.get("compressed_block", "")
        if not compressed_text or not compressed_text.strip():
            log.warning("Scratchpad block consolidation returned empty, skipping")
            return usage

        _write_knowledge_entries(knowledge_dir, result.get("knowledge_entries", []))
        _rebuild_knowledge_index(knowledge_dir)

        compressed_block = {
            "ts": utc_now_iso(),
            "source": "consolidation",
            "content": compressed_text.strip(),
        }
        new_blocks = [compressed_block] + recent_blocks

        _write_locked_json(memory.scratchpad_blocks_path(), new_blocks)
        memory.regenerate_scratchpad_md()

        log.info("Scratchpad blocks consolidated: %d blocks (%d chars) -> %d blocks (%d chars)",
                 len(blocks), total_chars,
                 len(new_blocks), sum(len(b.get("content", "")) for b in new_blocks))
        return usage

    except Exception as e:
        log.error("Scratchpad block consolidation failed: %s", e, exc_info=True)
        return None


def _write_knowledge_entries(knowledge_dir: pathlib.Path, entries: List[Dict[str, Any]]) -> None:
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        topic = entry.get("topic", "").strip()
        kb_content = entry.get("content", "").strip()
        if not topic or not kb_content:
            continue
        safe_topic = "".join(c for c in topic if c.isalnum() or c in "-_").lower()
        if not safe_topic:
            continue
        kb_path = knowledge_dir / f"{safe_topic}.md"
        existing = read_text(kb_path) if kb_path.exists() else ""
        write_text(kb_path, existing.rstrip() + "\n\n" + kb_content if existing else f"# {topic}\n\n{kb_content}\n")
