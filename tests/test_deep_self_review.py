"""Tests for ouroboros.deep_self_review module."""

from __future__ import annotations

import os
import pathlib
from unittest import mock

import pytest

from ouroboros.deep_self_review import (
    _is_probably_binary,
    build_review_pack,
    is_review_available,
    run_deep_self_review,
)


def _make_dulwich_mock(file_list: list[str]):
    """Return a mock for dulwich.repo.Repo that yields the given file list from open_index()."""
    mock_index = mock.Mock()
    mock_index.__iter__ = mock.Mock(return_value=iter(f.encode() for f in file_list))
    mock_repo = mock.Mock()
    mock_repo.open_index.return_value = mock_index
    mock_repo_cls = mock.Mock(return_value=mock_repo)
    return mock_repo_cls


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal git repo with tracked files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / "lib.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    return repo


@pytest.fixture
def tmp_drive(tmp_path):
    """Create a drive root with some memory files."""
    drive = tmp_path / "drive"
    drive.mkdir()
    mem = drive / "memory"
    mem.mkdir()
    (mem / "identity.md").write_text("I am Ouroboros.\n", encoding="utf-8")
    (mem / "scratchpad.md").write_text("Working notes.\n", encoding="utf-8")
    know = mem / "knowledge"
    know.mkdir()
    (know / "patterns.md").write_text("## Patterns\n- Error class A\n", encoding="utf-8")
    return drive


class TestBuildReviewPack:
    def test_reads_tracked_files(self, tmp_repo, tmp_drive):
        """git ls-files output determines which repo files are included."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "lib.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "### main.py" in pack
        assert "### lib.py" in pack
        assert "print('hello')" in pack
        assert stats["file_count"] >= 2

        atlas = mock.Mock(
            status="budget_exceeded",
            manifest={"estimated_total_tokens": 950_000},
            omitted=(),
            selected=(),
            text="small atlas",
        )
        with (
            mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])),
            mock.patch("ouroboros.deep_self_review.compile_review_context_atlas", return_value=atlas),
        ):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)
        assert pack == ""
        assert "exceeded hard budget" in stats["skipped"][0]
        assert stats["context_manifest"]["estimated_total_tokens"] == 950_000

    def test_includes_memory_whitelist(self, tmp_repo, tmp_drive):
        """Memory whitelist files from drive_root are included."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: drive/memory/identity.md" in pack
        assert "I am Ouroboros." in pack
        assert "## FILE: drive/memory/scratchpad.md" in pack
        assert "## FILE: drive/memory/knowledge/patterns.md" in pack

    def test_includes_improvement_backlog_when_present(self, tmp_repo, tmp_drive):
        (tmp_drive / "memory" / "knowledge" / "improvement-backlog.md").write_text(
            "# Improvement Backlog\n\n### ibl-1\n- summary: Fix recurring review blocker\n",
            encoding="utf-8",
        )
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
            pack, _stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: drive/memory/knowledge/improvement-backlog.md" in pack
        assert "Fix recurring review blocker" in pack

    def test_skips_missing_memory(self, tmp_repo, tmp_drive):
        """Missing memory files are silently skipped."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        # registry.md, WORLD.md, index-full.md don't exist — should not appear
        assert "registry.md" not in pack
        assert "WORLD.md" not in pack
        assert "index-full.md" not in pack


class TestIsReviewAvailable:
    def test_openrouter(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
            available, model = is_review_available()
        assert available is True
        assert model == "openai/gpt-5.5-pro"

    def test_openai(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        with mock.patch.dict(os.environ, env, clear=False):
            # Ensure OPENROUTER_API_KEY and OPENAI_BASE_URL are not set
            os.environ.pop("OPENROUTER_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)
            available, model = is_review_available()
        assert available is True
        assert model == "openai::gpt-5.5-pro"

    def test_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            available, model = is_review_available()
        assert available is False
        assert model is None


class TestRequestToolEmitsEvent:
    def test_emits_correct_event(self):
        """_request_deep_self_review emits a deep_self_review_request event."""
        from ouroboros.tools.control import _request_deep_self_review

        class FakeCtx:
            pending_events = []

        ctx = FakeCtx()
        with mock.patch(
            "ouroboros.deep_self_review.is_review_available",
            return_value=(True, "openai/gpt-5.5-pro"),
        ):
            result = _request_deep_self_review(ctx, "test reason")
        assert len(ctx.pending_events) == 1
        evt = ctx.pending_events[0]
        assert evt["type"] == "deep_self_review_request"
        assert evt["reason"] == "test reason"
        assert evt["model"] == "openai/gpt-5.5-pro"
        assert "Deep self-review" in result

    def test_unavailable_returns_error(self):
        """When no API key is available, returns error without emitting event."""
        from ouroboros.tools.control import _request_deep_self_review

        class FakeCtx:
            pending_events = []

        ctx = FakeCtx()
        with mock.patch(
            "ouroboros.deep_self_review.is_review_available",
            return_value=(False, None),
        ):
            result = _request_deep_self_review(ctx, "test reason")
        assert len(ctx.pending_events) == 0
        assert "unavailable" in result


class TestVendoredFilesExcluded:
    def test_minified_js_skipped(self, tmp_repo, tmp_drive):
        """Files with .min.js suffix are excluded from the review pack."""
        (tmp_repo / "lib.min.js").write_text("!function(){var a=1;}()\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "lib.min.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "vendored_minified" in str(stats["skipped"])
        assert "## FILE: lib.min.js" not in pack

    def test_chart_umd_skipped(self, tmp_repo, tmp_drive):
        """chart.umd.min.js (vendored Chart.js) is excluded by name and appears in OMITTED section."""
        (tmp_repo / "chart.umd.min.js").write_text("!function(t,e){/* chart.js minified */}()\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "chart.umd.min.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: chart.umd.min.js" not in pack
        assert any("chart.umd.min.js" in s for s in stats["skipped"])
        # Omission section must be present and mention the file
        assert "## OMITTED FILES" in pack
        assert "chart.umd.min.js" in pack

    def test_min_css_skipped(self, tmp_repo, tmp_drive):
        """Files with .min.css suffix are excluded."""
        (tmp_repo / "style.min.css").write_text("body{margin:0}a{color:red}\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "style.min.css"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: style.min.css" not in pack
        assert any("style.min.css" in s for s in stats["skipped"])

    def test_regular_js_included(self, tmp_repo, tmp_drive):
        """Regular (non-minified) JS files are NOT excluded."""
        (tmp_repo / "app.js").write_text("console.log('hello');\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "app.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "### app.js" in pack
        assert "console.log('hello');" in pack
        assert not any("app.js" in s for s in stats["skipped"])

    def test_omission_section_after_memory_whitelist(self, tmp_repo, tmp_drive):
        """OMITTED FILES section is appended after both repo and memory passes, capturing all skips.

        Simulates a memory-whitelist read error by patching pathlib.Path.read_text so that
        identity.md raises PermissionError, ensuring it lands in skipped and the OMITTED section.
        """
        (tmp_repo / "lib.min.js").write_text("minified\n")
        (tmp_drive / "memory" / "identity.md").write_text("I am Ouroboros.\n")
        target_path = str(tmp_drive / "memory" / "identity.md")

        original_read_text = pathlib.Path.read_text

        def patched_read_text(self, encoding="utf-8", errors="replace"):
            if str(self) == target_path:
                raise PermissionError("mocked read error")
            return original_read_text(self, encoding=encoding, errors=errors)

        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "lib.min.js"])):
            with mock.patch("pathlib.Path.read_text", patched_read_text):
                pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## OMITTED FILES" in pack
        omitted_section_pos = pack.index("## OMITTED FILES")
        # Vendored file listed in omitted section
        assert "lib.min.js" in pack[omitted_section_pos:]
        # Memory read error captured in skipped
        memory_errors = [s for s in stats["skipped"] if "identity.md" in s and "read error" in s]
        assert memory_errors, "identity.md read error should appear in skipped"
        # And it appears in the OMITTED section too
        assert "identity.md" in pack[omitted_section_pos:]


class TestIsProbablyBinary:
    def test_nul_byte_is_binary(self, tmp_path):
        """File containing a NUL byte is detected as binary."""
        f = tmp_path / "blob.bin"
        f.write_bytes(b"some text\x00more text")
        assert _is_probably_binary(f) is True

    def test_plain_text_is_not_binary(self, tmp_path):
        """Plain text file is not detected as binary."""
        f = tmp_path / "script.py"
        f.write_text("def hello():\n    return 'world'\n")
        assert _is_probably_binary(f) is False

    def test_high_non_printable_ratio_is_binary(self, tmp_path):
        """File with >30% non-printable bytes (ASCII control range) is detected as binary."""
        # 40% non-printable (bytes 1–8 range, ASCII control chars)
        payload = bytes(range(1, 9)) * 10 + b"normal text" * 3
        f = tmp_path / "data.unknown"
        f.write_bytes(payload)
        assert _is_probably_binary(f) is True

    def test_high_byte_ratio_is_binary(self, tmp_path):
        """File with invalid UTF-8 high bytes (no NUL) is detected as binary.

        bytes >= 128 alone are safe for valid UTF-8 (Cyrillic, CJK), but
        invalid UTF-8 sequences (e.g. raw Latin-1 bytes 0x80-0xFF) must still
        be caught by the incremental UTF-8 decode check.
        """
        # Raw Latin-1 bytes 0x80-0xFF: invalid UTF-8, no NUL, few control chars
        payload = bytes(range(128, 256)) * 5 + b"ascii text" * 5
        f = tmp_path / "data.blob"
        f.write_bytes(payload)
        assert _is_probably_binary(f) is True

    def test_only_first_sniff_bytes_read(self, tmp_path):
        """_is_probably_binary only reads _BINARY_SNIFF_BYTES bytes, not the whole file."""
        from ouroboros.deep_self_review import _BINARY_SNIFF_BYTES
        # File is mostly text but has NUL in the first 8KB window
        payload = b"text data\x00more" + b"a" * (_BINARY_SNIFF_BYTES * 2)
        f = tmp_path / "big.bin"
        f.write_bytes(payload)
        # Should detect NUL in the first chunk and return True
        assert _is_probably_binary(f) is True

    def test_empty_file_is_not_binary(self, tmp_path):
        """Empty file does not crash and returns False."""
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert _is_probably_binary(f) is False

    def test_missing_file_returns_false(self, tmp_path):
        """Missing file returns False (let caller handle read failure)."""
        f = tmp_path / "does_not_exist.bin"
        assert _is_probably_binary(f) is False

    def test_unlisted_extension_binary_excluded_from_pack(self, tmp_repo, tmp_drive):
        """Binary file with unlisted extension (.bin) is excluded via content sniffer."""
        (tmp_repo / "model.bin").write_bytes(b"GGUF\x00" + b"\x00\xff" * 100)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "model.bin"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: model.bin" not in pack
        assert any("model.bin" in s for s in stats["skipped"])


class TestBinaryFilesExcluded:
    def test_png_skipped(self, tmp_repo, tmp_drive):
        """PNG images are excluded — reading them produces garbage replacement chars."""
        (tmp_repo / "screenshot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "screenshot.png"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: screenshot.png" not in pack
        assert any("screenshot.png" in s for s in stats["skipped"])

    def test_jpg_skipped(self, tmp_repo, tmp_drive):
        """JPEG images are excluded."""
        (tmp_repo / "logo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "logo.jpg"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: logo.jpg" not in pack
        assert any("logo.jpg" in s for s in stats["skipped"])

    def test_svg_skipped(self, tmp_repo, tmp_drive):
        """SVG files are excluded (provider icons can be large XML)."""
        (tmp_repo / "icon.svg").write_text("<svg><circle r='10'/></svg>\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "icon.svg"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: icon.svg" not in pack
        assert any("icon.svg" in s for s in stats["skipped"])

    def test_ico_skipped(self, tmp_repo, tmp_drive):
        """ICO files are excluded."""
        (tmp_repo / "favicon.ico").write_bytes(b"\x00\x00\x01\x00" + b"\x00" * 50)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "favicon.ico"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: favicon.ico" not in pack
        assert any("favicon.ico" in s for s in stats["skipped"])

    def test_python_source_not_skipped(self, tmp_repo, tmp_drive):
        """Python source files (.py) are NOT excluded by the binary filter."""
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "### main.py" in pack


class TestSkipDirPrefixes:
    def test_assets_dir_excluded(self, tmp_repo, tmp_drive):
        """Files under assets/ are excluded (README screenshots, app icons)."""
        assets = tmp_repo / "assets"
        assets.mkdir()
        (assets / "chat.png").write_bytes(b"\x89PNG\r\n" + b"\x00" * 100)
        (assets / "logo.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 50)
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "assets/chat.png", "assets/logo.jpg"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "## FILE: assets/chat.png" not in pack
        assert "## FILE: assets/logo.jpg" not in pack
        assert any("assets/chat.png" in s for s in stats["skipped"])
        assert any("assets/logo.jpg" in s for s in stats["skipped"])
        assert "### main.py" in pack  # non-assets file still present

    def test_web_dir_not_excluded(self, tmp_repo, tmp_drive):
        """Files under web/ (SPA modules) are NOT excluded."""
        web = tmp_repo / "web" / "modules"
        web.mkdir(parents=True)
        (web / "chat.js").write_text("// chat module\n")
        with mock.patch("dulwich.repo.Repo", _make_dulwich_mock(["main.py", "web/modules/chat.js"])):
            pack, stats = build_review_pack(tmp_repo, tmp_drive)

        assert "### web/modules/chat.js" in pack
        assert not any("web/modules/chat.js" in s for s in stats["skipped"])


class TestNoProxyLlmChat:
    """LLMClient.chat(no_proxy=True) — proxy-free httpx transport for macOS fork-safety."""

    def test_chat_no_proxy_uses_trust_env_false(self):
        """chat(no_proxy=True) builds an httpx.Client with trust_env=False and mounts={}."""
        import httpx
        from ouroboros.llm import LLMClient

        captured_clients = []

        real_httpx_client = httpx.Client

        def capturing_httpx_client(*args, **kwargs):
            c = real_httpx_client(*args, **kwargs)
            captured_clients.append(c)
            return c

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

        with mock.patch("httpx.Client", side_effect=capturing_httpx_client):
            with mock.patch("openai.OpenAI") as mock_openai_cls:
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.return_value = mock_resp
                mock_openai_cls.return_value = mock_oa

                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                    llm.chat(
                        messages=[{"role": "user", "content": "hi"}],
                        model="openai/gpt-5.5-pro",
                        no_proxy=True,
                    )

        # At least one httpx.Client was created
        assert len(captured_clients) >= 1
        created = captured_clients[0]
        # trust_env=False and mounts={} are the key invariants
        assert created._mounts == {} or not created._mounts

    def test_chat_no_proxy_closes_http_client(self):
        """chat(no_proxy=True) closes the one-shot httpx.Client after the call."""
        import httpx
        from ouroboros.llm import LLMClient

        closed_clients = []
        real_httpx_client = httpx.Client

        class TrackingClient(real_httpx_client):
            def close(self):
                closed_clients.append(self)
                super().close()

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

        with mock.patch("httpx.Client", TrackingClient):
            with mock.patch("openai.OpenAI") as mock_openai_cls:
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.return_value = mock_resp
                mock_openai_cls.return_value = mock_oa

                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                    llm.chat(
                        messages=[{"role": "user", "content": "hi"}],
                        model="openai/gpt-5.5-pro",
                        no_proxy=True,
                    )

        assert len(closed_clients) >= 1, "httpx.Client must be closed after no_proxy call"

    def test_chat_no_proxy_closes_on_exception(self):
        """chat(no_proxy=True) closes the http client even when the API call raises."""
        import httpx
        from ouroboros.llm import LLMClient

        closed_clients = []
        real_httpx_client = httpx.Client

        class TrackingClient(real_httpx_client):
            def close(self):
                closed_clients.append(self)
                super().close()

        llm = LLMClient()

        with mock.patch("httpx.Client", TrackingClient):
            with mock.patch("openai.OpenAI") as mock_openai_cls:
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.side_effect = RuntimeError("boom")
                mock_openai_cls.return_value = mock_oa

                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                    with pytest.raises(RuntimeError, match="boom"):
                        llm.chat(
                            messages=[{"role": "user", "content": "hi"}],
                            model="openai/gpt-5.5-pro",
                            no_proxy=True,
                        )

        assert len(closed_clients) >= 1, "httpx.Client must be closed even after exception"

    def test_chat_no_proxy_skips_generation_cost_fetch(self):
        """chat(no_proxy=True) does not call _fetch_generation_cost (proxy/OS path)."""
        from ouroboros.llm import LLMClient

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "id": "gen-abc123",  # has a generation id — would trigger cost fetch normally
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        with mock.patch("httpx.Client") as mock_httpx_cls:
            mock_http = mock.Mock()
            mock_httpx_cls.return_value = mock_http
            with mock.patch("openai.OpenAI") as mock_openai_cls:
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.return_value = mock_resp
                mock_openai_cls.return_value = mock_oa
                with mock.patch.object(llm, "_fetch_generation_cost") as mock_cost:
                    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                        llm.chat(
                            messages=[{"role": "user", "content": "hi"}],
                            model="openai/gpt-5.5-pro",
                            no_proxy=True,
                        )
                    mock_cost.assert_not_called()

    def test_chat_no_proxy_false_uses_cached_client(self):
        """chat(no_proxy=False, default) uses the shared cached client, not a new one."""
        import httpx
        from ouroboros.llm import LLMClient

        new_clients = []
        real_httpx_client = httpx.Client

        def counting_httpx_client(*args, **kwargs):
            c = real_httpx_client(*args, **kwargs)
            new_clients.append(c)
            return c

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

        with mock.patch("httpx.Client", side_effect=counting_httpx_client):
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                with mock.patch.object(llm, "_get_remote_client") as mock_get:
                    mock_oa = mock.Mock()
                    mock_oa.chat.completions.create.return_value = mock_resp
                    mock_get.return_value = mock_oa
                    llm.chat(
                        messages=[{"role": "user", "content": "hi"}],
                        model="openai/gpt-5.5-pro",
                        no_proxy=False,
                    )
                    mock_get.assert_called_once()

        # no_proxy=False must not construct a new httpx.Client
        assert len(new_clients) == 0

    def test_run_deep_self_review_calls_llm_with_no_proxy(self, tmp_repo, tmp_drive):
        """run_deep_self_review passes no_proxy=True to llm.chat."""
        from ouroboros.deep_self_review import run_deep_self_review
        small_pack = "x" * 100
        manifest = {"status": "ok", "selected_count": 1}
        mock_llm = mock.Mock()
        mock_llm.chat.return_value = ({"content": "Review result."}, {"cost": 0.01})

        with mock.patch(
            "ouroboros.deep_self_review.build_review_pack",
            return_value=(
                small_pack,
                {
                    "file_count": 1,
                    "total_chars": len(small_pack),
                    "skipped": [],
                    "context_manifest": manifest,
                },
            ),
        ):
            result, usage = run_deep_self_review(
                repo_dir=tmp_repo,
                drive_root=tmp_drive,
                llm=mock_llm,
                emit_progress=lambda x: None,
                event_queue=None,
                model="openai/gpt-5.5-pro",
            )

        assert result == "Review result."
        mock_llm.chat.assert_called_once()
        _, kwargs = mock_llm.chat.call_args
        assert kwargs.get("no_proxy") is True, "llm.chat must be called with no_proxy=True"
        sidecar = tmp_drive / "state" / "deep_self_review_context.json"
        assert sidecar.is_file()
        assert '"context_manifest"' in sidecar.read_text(encoding="utf-8")
        assert '"selected_count": 1' in sidecar.read_text(encoding="utf-8")


class TestReviewPackOverflow:
    def test_explicit_error_on_overflow(self, tmp_repo, tmp_drive):
        """When pack exceeds ~920K tokens, run_deep_self_review returns an error."""
        # Create a pack that's way too large (> 3.68M chars ≈ 920K tokens)
        huge_pack = "x" * 4_000_000
        mock_llm = mock.Mock()

        with mock.patch(
            "ouroboros.deep_self_review.build_review_pack",
            return_value=(huge_pack, {"file_count": 100, "total_chars": 4_000_000, "skipped": []}),
        ):
            result, usage = run_deep_self_review(
                repo_dir=tmp_repo,
                drive_root=tmp_drive,
                llm=mock_llm,
                emit_progress=lambda x: None,
                event_queue=None,
                model="test-model",
            )

        assert "too large" in result
        assert "920,000" in result
        assert usage == {}
        mock_llm.chat.assert_not_called()
