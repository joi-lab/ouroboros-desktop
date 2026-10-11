import json

import pytest

from ouroboros.contracts.skill_manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    SkillManifestError,
    parse_skill_manifest_text,
)


def test_manifest_conflicts_are_optional_schema_v1_and_preserve_unknown_fields() -> None:
    manifest = parse_skill_manifest_text(
        """---
name: telegram
description: Telegram
version: 1.0.0
type: extension
entry: plugin.py
conflicts: telegram-bridge, legacy.telegram
future_field: preserved
---
body
"""
    )

    assert manifest.schema_version == SKILL_MANIFEST_SCHEMA_VERSION == 1
    assert manifest.conflicts == ["telegram-bridge", "legacy.telegram"]
    assert manifest.raw_extra == {"future_field": "preserved"}

    without_conflicts = parse_skill_manifest_text("# Body-only skill")
    assert without_conflicts.conflicts == []


def test_json_manifest_parses_conflicts_without_schema_bump() -> None:
    manifest = parse_skill_manifest_text(json.dumps({
        "name": "telegram",
        "description": "Telegram",
        "version": "1.0.0",
        "type": "extension",
        "entry": "plugin.py",
        "conflicts": ["telegram-bridge"],
    }))

    assert manifest.conflicts == ["telegram-bridge"]
    assert manifest.schema_version == 1


@pytest.mark.parametrize("name", ["../telegram", "telegram bridge", ".hidden", "x" * 65])
def test_manifest_rejects_noncanonical_conflict_names(name: str) -> None:
    with pytest.raises(SkillManifestError, match="canonical skill name"):
        parse_skill_manifest_text(
            """---
name: telegram
description: Telegram
version: 1.0.0
type: extension
entry: plugin.py
conflicts: [%s]
---
""" % name
        )


def test_manifest_parses_companion_processes_and_subscribe_events() -> None:
    manifest = parse_skill_manifest_text(
        """---
name: bridge
description: Bridge skill
version: 1.0.0
type: extension
entry: plugin.py
permissions: [companion_process, subscribe_event, inject_chat]
subscribe_events: [chat.outbound, chat.typing]
companion_processes:
  - name: daemon
    command: [python3, scripts/daemon.py]
    runtime: python3
---
# Bridge
"""
    )

    assert manifest.subscribe_events == ["chat.outbound", "chat.typing"]
    assert manifest.companion_processes[0]["name"] == "daemon"
    assert manifest.validate() == []


def test_manifest_warns_on_invalid_event_topic() -> None:
    manifest = parse_skill_manifest_text(
        """---
name: bridge
description: Bridge skill
version: 1.0.0
type: extension
entry: plugin.py
subscribe_events: [Chat.Outbound]
---
# Bridge
"""
    )

    assert any("invalid subscribe_events topic" in warning for warning in manifest.validate())


def test_manifest_rejects_unreviewed_companion_command_paths() -> None:
    bad = """---
name: bad
description: Bad
version: 1.0.0
type: extension
entry: plugin.py
companion_processes:
  - name: daemon
    command: [python3, /tmp/evil.py]
    runtime: python3
---
# Bad
"""

    try:
        parse_skill_manifest_text(bad)
    except Exception as exc:
        assert "reviewed skill tree" in str(exc)
    else:
        raise AssertionError("absolute companion command path should be rejected")


def test_manifest_rejects_unreviewed_subcommand_paths() -> None:
    bad = """---
name: bad
description: Bad
version: 1.0.0
type: extension
entry: plugin.py
companion_processes:
  - name: daemon
    command: [deno, run, /tmp/evil.ts]
    runtime: deno
---
# Bad
"""

    try:
        parse_skill_manifest_text(bad)
    except Exception as exc:
        assert "reviewed skill tree" in str(exc)
    else:
        raise AssertionError("absolute subcommand path should be rejected")


def test_manifest_rejects_inline_companion_flags_after_options() -> None:
    bad = """---
name: bad
description: Bad
version: 1.0.0
type: extension
entry: plugin.py
companion_processes:
  - name: daemon
    command: [python3, -u, -c, "print('x')"]
    runtime: python3
---
# Bad
"""

    try:
        parse_skill_manifest_text(bad)
    except Exception as exc:
        assert "inline/eval" in str(exc)
    else:
        raise AssertionError("inline companion command should be rejected")
