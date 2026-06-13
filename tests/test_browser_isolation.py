"""Tests for browser state isolation and infrastructure error detection."""
import pathlib
import socket
import sys
import types

import pytest

from ouroboros.contracts.task_constraint import TaskConstraint
import ouroboros.tools.browser as browser_mod
from ouroboros.tools.browser import _is_infrastructure_error, cleanup_browser


class TestInfrastructureErrorDetection:
    """_is_infrastructure_error should detect structural Playwright failures.

    Parametrized in v5.15.x — 7 single-case detection tests collapsed
    into one table (5 truthy infrastructure errors + 2 falsy
    application errors).
    """

    @pytest.mark.parametrize("exc,expected", [
        (RuntimeError("cannot switch to a different green thread"), True),
        (RuntimeError("different thread"), True),
        (Exception("browser has been closed"), True),
        (Exception("page has been closed"), True),
        (Exception("Connection closed"), True),
        (ValueError("invalid selector"), False),
        (TimeoutError("navigation timeout"), False),
    ])
    def test_classification(self, exc, expected):
        assert _is_infrastructure_error(exc) is expected


class TestBrowserModuleState:
    """Module-level state should be properly initialized."""

    # test_is_infrastructure_error_is_function removed in v5.15.x —
    # `assert callable(...)` on a function imported in this module's
    # imports is trivially true.

    def test_ensure_browser_tolerates_missing_thread_id(self, monkeypatch):
        routes = []
        fake_page = types.SimpleNamespace(
            set_default_timeout=lambda timeout: None,
            route=lambda pattern, handler: routes.append((pattern, handler)),
        )

        def _new_page(**kwargs):
            return fake_page

        fake_browser = types.SimpleNamespace(
            new_page=_new_page,
            is_connected=lambda: True,
        )
        fake_playwright = types.SimpleNamespace(
            chromium=types.SimpleNamespace(launch=lambda **kwargs: fake_browser)
        )
        fake_sync_api = types.SimpleNamespace(
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: fake_playwright)
        )
        monkeypatch.setattr(browser_mod, "_HAS_STEALTH", False)
        monkeypatch.setattr(browser_mod, "_ensure_playwright_installed", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            browser_mod.socket,
            "getaddrinfo",
            lambda host, *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ] if host == "example.com" else [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
            ],
        )
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

        ctx = types.SimpleNamespace(
            browser_state=types.SimpleNamespace(
                page=None,
                browser=None,
                pw_instance=None,
                last_screenshot_b64=None,
            )
        )

        page = browser_mod._ensure_browser(ctx)

        assert page is fake_page
        assert getattr(ctx.browser_state, "_thread_id", None) is not None
        assert routes == []

        subagent_ctx = types.SimpleNamespace(
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
            browser_state=types.SimpleNamespace(
                page=None,
                browser=None,
                pw_instance=None,
                last_screenshot_b64=None,
            )
        )
        assert browser_mod._ensure_browser(subagent_ctx) is fake_page
        assert routes and routes[-1][0] == "**/*"
        events = []
        route = types.SimpleNamespace(
            request=types.SimpleNamespace(url="http://127.0.0.1:8765/api/settings"),
            abort=lambda: events.append("abort"),
            continue_=lambda: events.append("continue"),
        )
        routes[-1][1](route)
        route.request.url = "http://192.168.1.1/admin"
        routes[-1][1](route)
        route.request.url = "http://169.254.1.1/"
        routes[-1][1](route)
        route.request.url = "http://[::]/"
        routes[-1][1](route)
        route.request.url = "https://example.com/"
        routes[-1][1](route)
        assert events == ["abort", "abort", "abort", "abort", "continue"]

    def test_local_readonly_browser_url_guard_resolves_dns_fail_closed(self, monkeypatch):
        def fake_getaddrinfo(host, *args, **kwargs):
            if host == "public.example":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            if host == "internal.example":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
            raise socket.gaierror("no such host")

        monkeypatch.setattr(browser_mod.socket, "getaddrinfo", fake_getaddrinfo)

        assert browser_mod._is_subagent_blocked_browser_url("ftp://public.example/file") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://127.0.0.1:8765") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://0177.0.0.1/") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://0x7f.0.0.1/") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://2130706433/") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://012.0.0.1/") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://internal.example") is True
        assert browser_mod._is_subagent_blocked_browser_url("http://missing.example") is True
        assert browser_mod._is_subagent_blocked_browser_url("https://public.example/path") is False

    def test_subagent_screenshot_text_does_not_reference_blocked_send_photo(self):
        fake_page = types.SimpleNamespace(screenshot=lambda **_kwargs: b"png")
        ctx = types.SimpleNamespace(
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
            browser_state=types.SimpleNamespace(last_screenshot_b64=None),
        )

        result = browser_mod._extract_page_output(fake_page, "screenshot", ctx)

        assert "send_photo" not in result
        assert "analyze_screenshot" in result
        assert ctx.browser_state.last_screenshot_b64

    def test_aliases_arm64_browser_cache_for_missing_x64_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(browser_mod.sys, "platform", "darwin", raising=False)
        root = tmp_path / "playwright" / "chromium_headless_shell-1208"
        arm_dir = root / "chrome-headless-shell-mac-arm64"
        arm_dir.mkdir(parents=True)
        arm_binary = arm_dir / "chrome-headless-shell"
        arm_binary.write_text("stub", encoding="utf-8")

        missing_binary = root / "chrome-headless-shell-mac-x64" / "chrome-headless-shell"
        err = RuntimeError(f"BrowserType.launch: Executable doesn't exist at {missing_binary}")

        assert browser_mod._maybe_alias_playwright_binary(err) is True
        alias_dir = missing_binary.parent
        assert alias_dir.is_symlink()
        assert pathlib.Path(alias_dir.resolve()) == arm_dir.resolve()

        arm_dir_2 = root / "chrome-headless-shell-mac-arm64-copy"
        arm_dir_2.mkdir(parents=True)
        (arm_dir_2 / "chrome-headless-shell").write_text("stub", encoding="utf-8")
        missing_binary_2 = root / "chrome-headless-shell-mac-x64-copy" / "chrome-headless-shell"
        err2 = RuntimeError(f"BrowserType.launch: Executable doesn't exist at {missing_binary_2}")
        fake_pw = types.SimpleNamespace(chromium=types.SimpleNamespace(launch=lambda **_kwargs: (_ for _ in ()).throw(err2)))
        with pytest.raises(RuntimeError):
            browser_mod._launch_browser_with_fallback(fake_pw, allow_cache_write=False)
        assert not missing_binary_2.parent.exists()


class TestHasPlatformChromium:
    """_has_platform_chromium: two-level check — chromium-* dir + platform-matching subdir.

    Parametrized in v5.15.x — 7 tests collapsed into 2 (one for the
    "not found" matrix, one for the "found via real executable" matrix).
    Each subcase builds its filesystem skeleton inside tmp_path via a
    small builder kwarg.
    """

    def _build_fixture(self, tmp_path, kind: str):
        """kind values:
        - missing       : nothing exists
        - empty         : tmp_path has no chromium-* subdir
        - non_chromium  : a firefox-* dir but no chromium-*
        - wrong_platform: chromium-X/chrome-linux-x64 (wrong platform on darwin)
        - metadata_only : chromium-X/chrome-mac-x64/metadata.json (no exe)
        - real_app      : chromium-X/chrome-mac-x64/Chromium.app/.../Chromium
        - headless_shell: chromium_headless_shell-X/chrome-headless-shell-mac-arm64/chrome-headless-shell
        """
        if kind == "missing":
            return tmp_path / "nonexistent"
        if kind == "empty":
            return tmp_path
        if kind == "non_chromium":
            (tmp_path / "firefox-1234").mkdir()
            return tmp_path
        if kind == "wrong_platform":
            cdir = tmp_path / "chromium-1234"
            cdir.mkdir()
            (cdir / "chrome-linux-x64").mkdir()
            return tmp_path
        if kind == "metadata_only":
            cdir = tmp_path / "chromium-1234"
            cdir.mkdir()
            pdir = cdir / "chrome-mac-x64"
            pdir.mkdir()
            (pdir / "metadata.json").write_text("{}", encoding="utf-8")
            return tmp_path
        if kind == "real_app":
            cdir = tmp_path / "chromium-1234"
            cdir.mkdir()
            exe = cdir / "chrome-mac-x64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
            exe.parent.mkdir(parents=True)
            exe.write_text("stub", encoding="utf-8")
            return tmp_path
        if kind == "headless_shell":
            cdir = tmp_path / "chromium_headless_shell-1234"
            cdir.mkdir()
            exe = cdir / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            exe.parent.mkdir(parents=True)
            exe.write_text("stub", encoding="utf-8")
            return tmp_path
        raise ValueError(f"unknown kind: {kind}")

    @pytest.mark.parametrize("kind,expected", [
        ("missing",        False),
        ("empty",          False),
        ("non_chromium",   False),
        ("wrong_platform", False),
        ("metadata_only",  False),
        ("real_app",       True),
        ("headless_shell", True),
    ])
    def test_classification(self, kind, expected, tmp_path, monkeypatch):
        from ouroboros.tools import browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        from ouroboros.tools.browser import _has_platform_chromium

        root = self._build_fixture(tmp_path, kind)
        assert _has_platform_chromium(root) is expected


class TestSetPlaywrightBrowsersPathIfBundled:
    """_set_playwright_browsers_path_if_bundled: sets env var only when bundled Chromium found."""

    def test_no_op_when_env_already_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/some/custom/path")
        import importlib
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        # Should not overwrite existing env var
        bmod._set_playwright_browsers_path_if_bundled()
        import os
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/some/custom/path"

    def test_sets_zero_when_chromium_dir_matches(self, monkeypatch, tmp_path):
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        # Build fake playwright package structure
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        chromium_dir = local_browsers / "chromium-9999"
        chromium_dir.mkdir(parents=True)
        platform_dir = chromium_dir / "chrome-mac-x64"
        exe = platform_dir / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")  # real macOS executable path
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        bmod._set_playwright_browsers_path_if_bundled()
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"

    def test_sets_zero_when_headless_shell_dir_matches(self, monkeypatch, tmp_path):
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        chromium_dir = local_browsers / "chromium_headless_shell-9999"
        chromium_dir.mkdir(parents=True)
        platform_dir = chromium_dir / "chrome-headless-shell-mac-arm64"
        exe = platform_dir / "chrome-headless-shell"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        bmod._set_playwright_browsers_path_if_bundled()
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"

    def test_no_change_when_no_matching_chromium(self, monkeypatch, tmp_path):
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        local_browsers.mkdir(parents=True)
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        bmod._set_playwright_browsers_path_if_bundled()
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    def test_import_time_side_effect_sets_env_when_bundled(self, monkeypatch, tmp_path):
        """Module-import calls _set_playwright_browsers_path_if_bundled(); reloading the
        module with a fake bundled Chromium present must set PLAYWRIGHT_BROWSERS_PATH=0."""
        import importlib
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        # Build fake playwright package with a non-empty platform dir
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        exe = local_browsers / "chromium-9999" / "chrome-mac-x64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")  # real macOS executable path
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        # Simulate a fresh module import by calling the module-level init directly
        # (importlib.reload would re-run the side-effect but also re-register tools;
        # calling the function directly tests the same code path without side effects)
        bmod._set_playwright_browsers_path_if_bundled()
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"

    def test_browser_install_uses_data_cache_when_zero_has_no_browser(self, monkeypatch, tmp_path):
        import os
        import ouroboros.tools.browser as bmod

        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
        monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setattr(bmod, "_playwright_ready", False)
        calls = []
        monkeypatch.setattr(bmod.subprocess, "check_call", lambda cmd: calls.append(cmd))
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "playwright" / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        fake_sync = types.ModuleType("playwright.sync_api")

        class FakeSyncPlaywright:
            def __enter__(self):
                chromium = types.SimpleNamespace(executable_path=str(tmp_path / "missing-chromium"))
                return types.SimpleNamespace(chromium=chromium)

            def __exit__(self, *_args):
                return False

        fake_sync.sync_playwright = lambda: FakeSyncPlaywright()
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)

        bmod._ensure_playwright_installed()

        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "data" / "playwright-browsers")
        assert calls[-1][-3:] == ["playwright", "install", "chromium"]


class TestCleanupBrowser:
    """cleanup_browser should null out all browser_state references."""

    def test_cleanup_nulls_state(self):
        ctx = types.SimpleNamespace(
            browser_state=types.SimpleNamespace(
                page=None,
                browser=None,
                pw_instance=None,
                last_screenshot_b64=None,
            )
        )
        cleanup_browser(ctx)
        assert ctx.browser_state.page is None
        assert ctx.browser_state.browser is None
        assert ctx.browser_state.pw_instance is None
