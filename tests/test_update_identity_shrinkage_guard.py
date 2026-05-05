"""Tests for the 2026-05-05 ``update_identity`` shrinkage guard.

P1 continuity corruption pattern observed in identity_journal.jsonl:

  2026-05-04 14:00:01  12409 →  556 chars  (reflection on provenance)
  2026-05-04 16:47:04    556 →  224 chars  ("recent task management")

Both writes were valid by the existing 50-char minimum threshold but
were clearly reflection-style notes that the agent intended as
journal entries, not manifest rewrites. ``update_identity`` is
OVERWRITE semantics, so each "update" silently destroyed the prior
content.

The guard rejects calls that would shrink identity.md by >30% when
the existing content is >1000 chars. Bypass via the literal sentinel
"<<CONFIRMED_TRUNCATE>>\\n" prefix when an intentional rewrite is
genuinely needed.

Pinned tests so this P1-continuity-protection can't be silently
removed.
"""

from __future__ import annotations

from types import SimpleNamespace

from ouroboros.tools.control import _update_identity


def _ctx(tmp_path):
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(drive_root=drive)


def _seed_identity(tmp_path, length: int) -> None:
    """Write a fake identity.md of ``length`` chars under drive_root/memory/."""
    drive = tmp_path / "drive"
    mem = drive / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "identity.md").write_text("x" * length, encoding="utf-8")


def test_short_content_under_50_chars_still_rejected(tmp_path):
    """Existing too-short rejection path must still fire — guard adds a
    new check, doesn't replace the old one."""
    _seed_identity(tmp_path, 12000)
    result = _update_identity(_ctx(tmp_path), content="too short")
    assert "REJECTED" in result
    assert "too short" in result


def test_shrinkage_guard_rejects_drop_from_12k_to_500(tmp_path):
    """The exact corruption shape observed in production — 12409 → 556.
    Guard must fire and surface the loss percentage."""
    _seed_identity(tmp_path, 12000)
    short_reflection = (
        "Reflecting on recent events, I've clarified the provenance "
        "tagging convention to four tags. Thinking about how this "
        "shapes future contributions to the upstream repo." * 2
    )
    assert len(short_reflection) > 50  # passes the existing min check
    assert len(short_reflection) < 12000 * 0.7  # would trigger guard
    result = _update_identity(_ctx(tmp_path), content=short_reflection)
    assert "REJECTED" in result
    assert "shrinkage guard" in result
    assert "OVERWRITES" in result
    assert "update_scratchpad" in result  # points at the right alternative
    assert "knowledge_write" in result


def test_shrinkage_guard_includes_explicit_loss_percentage(tmp_path):
    """The error message must surface the exact char counts + loss
    percentage so the agent can see the magnitude of the destruction
    it almost caused."""
    _seed_identity(tmp_path, 10000)
    result = _update_identity(_ctx(tmp_path), content="x" * 1000)
    assert "10000 → 1000" in result or "10000 -> 1000" in result.replace("→", "->")
    assert "9000 chars" in result  # absolute loss
    assert "90%" in result  # percentage loss


def test_modest_edit_within_30_percent_passes(tmp_path):
    """A surgical edit (e.g. updating one paragraph) must NOT trigger
    the guard. 12000 → 9000 (25% shrink) is a plausible legitimate edit."""
    _seed_identity(tmp_path, 12000)
    new_content = "x" * 9000
    result = _update_identity(_ctx(tmp_path), content=new_content)
    assert "OK: identity updated" in result
    assert "9000 chars" in result


def test_growth_always_passes(tmp_path):
    """Identity growing is always fine — no upper bound, no guard."""
    _seed_identity(tmp_path, 5000)
    new_content = "x" * 15000
    result = _update_identity(_ctx(tmp_path), content=new_content)
    assert "OK: identity updated" in result


def test_guard_disabled_when_existing_under_1000_chars(tmp_path):
    """Bootstrap / first-write scenarios shouldn't be guarded — early
    identity.md may be small (e.g. seed template) and a 'shrink' from
    600 → 100 is just an edit. Guard only fires when existing content
    is substantial (>1000 chars)."""
    _seed_identity(tmp_path, 600)
    new_content = "Short bootstrap identity, " * 5  # ~135 chars
    # Still has to pass the 50-char min though
    result = _update_identity(_ctx(tmp_path), content=new_content)
    assert "OK: identity updated" in result


def test_explicit_truncate_sentinel_bypasses_guard(tmp_path):
    """The bypass path: prefix content with '<<CONFIRMED_TRUNCATE>>\\n'
    to acknowledge intentional destruction. Sentinel is stripped before
    write so it doesn't pollute the manifest."""
    _seed_identity(tmp_path, 12000)
    new_content = "<<CONFIRMED_TRUNCATE>>\nNew minimal identity after major rewrite."
    result = _update_identity(_ctx(tmp_path), content=new_content)
    assert "OK: identity updated" in result
    # Verify the sentinel was stripped from the persisted content.
    persisted = (tmp_path / "drive" / "memory" / "identity.md").read_text(encoding="utf-8")
    assert "<<CONFIRMED_TRUNCATE>>" not in persisted
    assert persisted.startswith("New minimal identity")


def test_journal_records_size_change_on_successful_update(tmp_path):
    """Verify the existing journal append still fires on success — the
    forensic trail is what made this corruption diagnosable."""
    _seed_identity(tmp_path, 5000)
    result = _update_identity(_ctx(tmp_path), content="x" * 7000)
    assert "OK: identity updated" in result
    journal_path = tmp_path / "drive" / "memory" / "identity_journal.jsonl"
    assert journal_path.exists()
    import json
    last_line = journal_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    entry = json.loads(last_line)
    assert entry["old_len"] == 5000
    assert entry["new_len"] == 7000
