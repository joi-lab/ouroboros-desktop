"""Tests for prompt cache layout and determinism."""

import pathlib
import tempfile


def _make_env_and_memory(tmpdir: pathlib.Path):
    from ouroboros.agent import Env
    from ouroboros.memory import Memory

    repo_dir = tmpdir / "repo"
    drive_root = tmpdir / "drive"
    repo_dir.mkdir(parents=True, exist_ok=True)
    drive_root.mkdir(parents=True, exist_ok=True)
    for subdir in ["drive/state", "drive/memory", "drive/memory/knowledge", "drive/logs", "repo/docs", "repo/prompts"]:
        (tmpdir / subdir).mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts" / "SYSTEM.md").write_text("You are Ouroboros.", encoding="utf-8")
    (repo_dir / "BIBLE.md").write_text("# Principle 0: Agency", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v1.2.3 — Architecture", encoding="utf-8")
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text("# DEVELOPMENT.md", encoding="utf-8")
    (repo_dir / "README.md").write_text("version-1.2.3", encoding="utf-8")
    (repo_dir / "docs" / "CHECKLISTS.md").write_text("## Repo Commit Checklist", encoding="utf-8")
    (drive_root / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (drive_root / "memory" / "scratchpad.md").write_text("scratch", encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("identity", encoding="utf-8")
    env = Env(repo_dir=repo_dir, drive_root=drive_root)
    memory = Memory(drive_root=drive_root, repo_dir=repo_dir)
    return env, memory


def test_build_llm_messages_returns_three_system_blocks():
    from ouroboros.context import build_llm_messages

    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env, memory = _make_env_and_memory(tmpdir)
    messages, _ = build_llm_messages(env=env, memory=memory, task={"id": "t1", "type": "task", "text": "hi"})
    system_msg = messages[0]
    assert system_msg["role"] == "system"
    assert isinstance(system_msg["content"], list)
    assert len(system_msg["content"]) == 3
    assert system_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert system_msg["content"][1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system_msg["content"][2]


def test_build_llm_messages_repartitions_stable_vs_dynamic_sections():
    from ouroboros.context import build_llm_messages

    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env, memory = _make_env_and_memory(tmpdir)
    (tmpdir / "drive" / "memory" / "dialogue_blocks.json").write_text(
        '[{"content": "dialogue"}]',
        encoding="utf-8",
    )
    (tmpdir / "drive" / "memory" / "registry.md").write_text(
        "### source-a\n- **path:** memory/registry.md\n- **updated:** 2026-04-13T10:00:00+00:00\n- **gaps:** none\n",
        encoding="utf-8",
    )
    (tmpdir / "drive" / "memory" / "deep_review.md").write_text("deep review", encoding="utf-8")
    (tmpdir / "drive" / "memory" / "knowledge" / "index-full.md").write_text("kb", encoding="utf-8")
    (tmpdir / "drive" / "memory" / "knowledge" / "patterns.md").write_text("patterns", encoding="utf-8")

    messages, _ = build_llm_messages(env=env, memory=memory, task={"id": "t2", "type": "task", "text": "hi"})
    stable_text = messages[0]["content"][1]["text"]
    dynamic_text = messages[0]["content"][2]["text"]

    assert "## Identity" in stable_text
    assert "## Knowledge base" in stable_text
    assert "## Known error patterns (Pattern Register)" in stable_text
    assert "## Last Deep Self-Review" in stable_text
    assert "## Scratchpad" not in stable_text
    assert "## Dialogue History" not in stable_text
    assert "## Dialogue Summary" not in stable_text
    assert "## Memory Registry" not in stable_text

    assert "## Scratchpad" in dynamic_text
    assert ("## Dialogue Summary" in dynamic_text) or ("## Dialogue History" in dynamic_text)
    assert "## Memory Registry (what I know / don't know)" in dynamic_text
    assert "## Memory Registry\n\n" not in dynamic_text
    assert "## Memory Registry (what I know / don't know)" not in stable_text
    assert "## Last Deep Self-Review" not in dynamic_text


def test_sanitize_chat_completion_tools_sorts_by_name():
    from ouroboros.llm import LLMClient

    tools = [
        {"type": "function", "function": {"name": "zeta_tool", "description": "z", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "alpha_tool", "description": "a", "parameters": {"type": "object", "properties": {}}}},
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool["function"]["name"] for tool in sanitized] == ["alpha_tool", "zeta_tool"]


def test_sanitize_chat_completion_tools_deduplicates_before_sorting():
    from ouroboros.llm import LLMClient

    tools = [
        {"type": "function", "function": {"name": "beta_tool", "description": "first", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "alpha_tool", "description": "alpha", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "beta_tool", "description": "second", "parameters": {"type": "object", "properties": {}}}},
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool["function"]["name"] for tool in sanitized] == ["alpha_tool", "beta_tool"]
    assert sanitized[1]["function"]["description"] == "first"


def test_sanitize_chat_completion_tools_drops_provider_invalid_names():
    from ouroboros.llm import LLMClient

    tools = [
        {"type": "function", "function": {"name": "ext.weather.fetch", "description": "bad", "parameters": {}}},
        {"type": "function", "function": {"name": "ext_9_r_weather_fetch", "description": "ok", "parameters": {}}},
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool["function"]["name"] for tool in sanitized] == ["ext_9_r_weather_fetch"]


def test_sanitize_chat_completion_tools_drops_overlong_names():
    from ouroboros.llm import LLMClient

    tools = [
        {"type": "function", "function": {"name": "a" * 65, "description": "bad", "parameters": {}}},
        {"type": "function", "function": {"name": "a" * 64, "description": "ok", "parameters": {}}},
    ]

    sanitized = LLMClient._sanitize_chat_completion_tools(tools)
    assert [tool["function"]["name"] for tool in sanitized] == ["a" * 64]


def test_build_remote_kwargs_marks_last_sorted_tool_for_cache():
    from ouroboros.llm import LLMClient

    client = LLMClient()
    target = client._resolve_remote_target("anthropic/claude-sonnet-4.6")
    kwargs = client._build_remote_kwargs(
        target,
        [{"role": "user", "content": "hi"}],
        "high",
        512,
        "auto",
        None,
        [
            {"type": "function", "function": {"name": "zeta_tool", "description": "z", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "alpha_tool", "description": "a", "parameters": {"type": "object", "properties": {}}}},
        ],
    )

    assert [tool["function"]["name"] for tool in kwargs["tools"]] == ["alpha_tool", "zeta_tool"]
    assert "cache_control" not in kwargs["tools"][0]
    assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}


def test_build_memory_sections_partition_modes():
    from ouroboros.context import build_memory_sections

    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env, memory = _make_env_and_memory(tmpdir)
    (tmpdir / "drive" / "memory" / "dialogue_blocks.json").write_text(
        '[{"content": "dialogue"}]',
        encoding="utf-8",
    )
    (tmpdir / "drive" / "memory" / "registry.md").write_text(
        "### source-a\n- **path:** memory/registry.md\n- **updated:** 2026-04-13T10:00:00+00:00\n- **gaps:** none\n",
        encoding="utf-8",
    )

    stable = build_memory_sections(memory, partition="stable")
    volatile = build_memory_sections(memory, partition="volatile")
    all_sections = build_memory_sections(memory, partition="all")
    registry_digest = __import__("ouroboros.context", fromlist=["_build_registry_digest"])._build_registry_digest(env)

    assert any(section.startswith("## Identity") for section in stable)
    assert not any(section.startswith("## Scratchpad") for section in stable)
    assert any(section.startswith("## Scratchpad") for section in volatile)
    assert any(
        section.startswith("## Dialogue Summary") or section.startswith("## Dialogue History")
        for section in volatile
    )
    assert not any(section.startswith("## Memory Registry") for section in volatile)
    assert registry_digest.startswith("## Memory Registry (what I know / don't know)")
    assert any(section.startswith("## Identity") for section in all_sections)
    assert any(section.startswith("## Scratchpad") for section in all_sections)


def test_llm_round_event_exposes_cache_hit_rate(tmp_path):
    from ouroboros.loop_llm_call import call_llm_with_retry

    class _CacheReportingLLM:
        def chat(self, **kwargs):
            return {"content": "ok"}, {
                "provider": "openrouter",
                "resolved_model": "anthropic/claude-sonnet-4.6",
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "cached_tokens": 750,
                "cache_write_tokens": 0,
                "prompt_cache_ttl": "default",
                "cost": 1.23,
            }

    usage = {}
    msg, cost = call_llm_with_retry(
        _CacheReportingLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic/claude-sonnet-4.6",
        None,
        "medium",
        1,
        tmp_path,
        "task-cache",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg == {"content": "ok"}
    assert cost == 1.23
    lines = [line for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    llm_round = next(__import__("json").loads(line) for line in lines if __import__("json").loads(line).get("type") == "llm_round")
    assert llm_round["cache_hit_rate"] == 0.75
    assert llm_round["prompt_cache_ttl"] == "default"


def test_llm_round_event_zero_prompt_tokens_reports_zero_hit_rate(tmp_path):
    from ouroboros.loop_llm_call import call_llm_with_retry

    class _ZeroPromptLLM:
        def chat(self, **kwargs):
            return {"content": "ok"}, {
                "provider": "openrouter",
                "resolved_model": "anthropic/claude-sonnet-4.6",
                "prompt_tokens": 0,
                "completion_tokens": 10,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "cost": 0.0,
            }

    usage = {}
    call_llm_with_retry(
        _ZeroPromptLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic/claude-sonnet-4.6",
        None,
        "medium",
        1,
        tmp_path,
        "task-cache-zero",
        1,
        None,
        usage,
        "task",
        False,
    )

    lines = [line for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    llm_round = next(__import__("json").loads(line) for line in lines if __import__("json").loads(line).get("type") == "llm_round")
    assert llm_round["cache_hit_rate"] == 0.0
