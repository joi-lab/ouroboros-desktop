"""Atlas-backed deep self-review against BIBLE.md using a large-context model."""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Pack filtering is shared with scope review.
from ouroboros.tools.review_context_atlas import (  # noqa: E402
    ReviewContextAtlasRequest,
    compile_review_context_atlas,
)
from ouroboros.tools.review_helpers import (  # noqa: E402
    _BINARY_SNIFF_BYTES,
    _MAX_FULL_REPO_FILE_BYTES,
    _is_probably_binary,
)
from ouroboros.utils import atomic_write_json, estimate_tokens, utc_now_iso  # noqa: E402

# Non-agent visual assets.
_SKIP_DIR_PREFIXES = (
    "assets/",
)

_MEMORY_WHITELIST = [
    "memory/identity.md",
    "memory/scratchpad.md",
    "memory/registry.md",
    "memory/WORLD.md",
    "memory/knowledge/index-full.md",
    "memory/knowledge/patterns.md",
    "memory/knowledge/improvement-backlog.md",
]

_SYSTEM_PROMPT = """\
You are conducting a deep self-review of the Ouroboros project — a self-creating AI agent.

Primary directive: The Constitution (BIBLE.md) is your absolute reference.
Every finding must be checked against it.

What to look for: bugs, crashes, race conditions,
BIBLE.md violations (P0–P12), contradictions between code and docs,
security gaps, dead code, missing error handling, architectural issues,
known error patterns from patterns.md that remain unfixed, and ideas how to improve Ouroboros to work better and better comply with the Bible.

How to work: Use the generated atlas coverage manifest systematically. Raw code is
included for selected functional/protected surfaces; every tracked file is still
accounted for by hash, size, classification, and omission/manifest disposition.
Cross-reference interactions between modules. Prioritize: CRITICAL > IMPORTANT > ADVISORY.

Output: Structured report with prioritized findings, each citing the
specific file, line/section, the problem, and the proposed fix."""


def _dulwich_tracked_paths(repo_dir: pathlib.Path) -> tuple[list[str], list[str]]:
    """Return git-tracked paths through dulwich for macOS fork safety."""
    try:
        import dulwich.repo as _dulwich_repo  # local import — avoid top-level cost if unused
        _repo = _dulwich_repo.Repo(str(repo_dir))
        tracked = sorted(p.decode("utf-8", errors="replace") for p in _repo.open_index())
        if not tracked:
            raise RuntimeError("dulwich index is empty — cannot build review pack")
        return tracked, []
    except ImportError:
        return [], ["FATAL: dulwich not installed. Run: pip install dulwich"]
    except Exception as exc:
        return [], [f"FATAL: {exc}"]


def _append_memory_whitelist(
    parts: list[str],
    skipped: list[str],
    *,
    drive_root: pathlib.Path,
) -> int:
    file_count = 0
    for rel_mem in _MEMORY_WHITELIST:
        full_path = drive_root / rel_mem
        try:
            if not full_path.is_file():
                continue
            size = full_path.stat().st_size
            if size > _MAX_FULL_REPO_FILE_BYTES:
                skipped.append(f"drive/{rel_mem} (>{_MAX_FULL_REPO_FILE_BYTES // 1024}KB)")
                continue
            content = full_path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                continue
            parts.append(f"## FILE: drive/{rel_mem}\n{content}\n")
            file_count += 1
        except Exception as exc:
            skipped.append(f"drive/{rel_mem} (read error: {exc})")
    return file_count


def _append_omission_section(parts: list[str], skipped: list[str]) -> None:
    if not skipped:
        return
    omission_lines = [
        "## OMITTED FILES (not included in review pack)",
        "These files were excluded. Reasons: sensitive=secrets/keys, "
        "vendored/minified=third-party bundled asset, binary/media=images/fonts/compiled blobs, "
        "excluded_dir=non-agent-logic directory, excluded_test=wider tests excluded, "
        "oversized=>1MB, read_error=unreadable, budget_omitted=required atlas file did not fit.",
        "",
    ]
    omission_lines.extend(f"  - {entry}" for entry in skipped)
    parts.append("\n".join(omission_lines) + "\n")


def build_review_pack(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    fixed_prompt_tokens: int = 0,
) -> Tuple[str, Dict[str, Any]]:
    """Build bounded repo atlas + full memory whitelist pack."""
    tracked, fatal = _dulwich_tracked_paths(repo_dir)
    if fatal:
        return "", {"file_count": 0, "total_chars": 0, "skipped": fatal}

    skipped: list[str] = []
    memory_parts: list[str] = []
    memory_count = _append_memory_whitelist(memory_parts, skipped, drive_root=drive_root)
    memory_text = "\n".join(memory_parts)
    atlas_fixed_tokens = int(fixed_prompt_tokens) + estimate_tokens(memory_text)
    atlas = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=repo_dir,
            tracked_paths=tuple(tracked),
            fixed_prompt_tokens=atlas_fixed_tokens,
            target_total_tokens=850_000,
            hard_total_tokens=920_000,
            include_tests=False,
            title="Generated Deep Self-Review Atlas",
        )
    )
    if atlas.status == "budget_exceeded":
        return "", {
            "file_count": 0,
            "total_chars": 0,
            "skipped": ["FATAL: generated repository atlas exceeded hard budget"],
            "context_manifest": atlas.manifest,
        }
    skipped.extend(
        f"{record.rel_path} ({record.disposition}: {record.reason})"
        for record in atlas.omitted
        if record.disposition not in {"already_included", "manifest_only"}
    )
    parts = [atlas.text]
    parts.extend(memory_parts)
    file_count = len(atlas.selected) + memory_count
    _append_omission_section(parts, skipped)

    pack_text = "\n".join(parts)
    stats = {
        "file_count": file_count,
        "total_chars": len(pack_text),
        "skipped": skipped,
        "context_manifest": atlas.manifest,
    }
    return pack_text, stats


def is_review_available() -> Tuple[bool, Optional[str]]:
    """Return whether a suitable large-context review model is configured."""
    if os.environ.get("OPENROUTER_API_KEY"):
        return True, "openai/gpt-5.5-pro"
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("OPENAI_BASE_URL"):
        return True, "openai::gpt-5.5-pro"
    return False, None


def run_deep_self_review(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    llm: Any,
    emit_progress: Callable[[str], None],
    event_queue: Any,
    model: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Execute full-project deep review; return error text instead of raising.

    no_proxy=True avoids macOS fork-safety SIGSEGV by using a one-shot httpx
    client with trust_env=False in llm.py; regular task calls are unaffected.
    """
    try:
        emit_progress("Building generated review atlas and memory pack...")
        pack_text, stats = build_review_pack(
            repo_dir,
            drive_root,
            fixed_prompt_tokens=estimate_tokens(_SYSTEM_PROMPT),
        )
        if not pack_text and stats.get("skipped"):
            return f"❌ Failed to build review pack: {stats['skipped'][0]}", {}

        emit_progress(
            f"Review pack built: {stats['file_count']} files, "
            f"{stats['total_chars']:,} chars"
            + (f", {len(stats['skipped'])} skipped" if stats["skipped"] else "")
        )

        # Gate full system+pack like scope/plan review; chars/4 undercounts near
        # the 1M window, so the prompt budget remains a best-effort guard.
        from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET
        full_prompt_chars = len(_SYSTEM_PROMPT) + len(pack_text)
        estimated_tokens = estimate_tokens(_SYSTEM_PROMPT + pack_text)
        if estimated_tokens > REVIEW_PROMPT_TOKEN_BUDGET:
            return (
                f"❌ Review pack too large: ~{estimated_tokens:,} tokens "
                f"({full_prompt_chars:,} chars of system+pack, {stats['file_count']} files). "
                f"Maximum is ~{REVIEW_PROMPT_TOKEN_BUDGET:,} tokens. Reduce codebase size or split review."
            ), {}

        if not model:
            available, model = is_review_available()
            if not available:
                return "❌ Deep self-review unavailable: no OPENROUTER_API_KEY or OPENAI_API_KEY configured.", {}

        if stats.get("context_manifest"):
            try:
                atomic_write_json(
                    drive_root / "state" / "deep_self_review_context.json",
                    {
                        "ts": utc_now_iso(),
                        "model": model,
                        "context_manifest": stats["context_manifest"],
                    },
                    trailing_newline=True,
                )
            except Exception:
                log.warning("Failed to persist deep self-review context manifest", exc_info=True)

        emit_progress(f"Sending to {model} (~{estimated_tokens:,} tokens). This may take several minutes...")

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": pack_text},
        ]

        # no_proxy prevents macOS fork-safety SIGSEGV in bundled child process.
        from ouroboros.llm_observability import chat_observed

        response, usage = chat_observed(
            llm,
            drive_root=drive_root,
            task_id="deep_self_review",
            call_type="deep_self_review",
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort="high",
            max_tokens=100_000,
            temperature=None,
            no_proxy=True,
        )

        text = response.get("content") or ""
        if not text:
            return "⚠️ Model returned an empty response for the deep self-review.", usage or {}

        emit_progress(f"Deep self-review complete ({len(text):,} chars).")
        return text, usage or {}

    except Exception as e:
        log.error("Deep self-review failed: %s", e, exc_info=True)
        return f"❌ Deep self-review failed: {type(e).__name__}: {e}", {}
