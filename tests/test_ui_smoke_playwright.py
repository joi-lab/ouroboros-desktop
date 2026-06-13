from __future__ import annotations

import os
import json
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from tests.fixtures_mock_llm import MockLLMServer


REPO_ROOT = os.path.dirname(os.path.dirname(__file__))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_health(url: str, timeout_sec: int = 30) -> None:
    deadline = time.time() + timeout_sec
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=2) as resp:  # noqa: S310 - local test server
                if resp.status == 200:
                    return
        except Exception as exc:
            last = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"server did not become healthy: {last}")


def _run_core_ui_assertions(url: str) -> None:
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector("#page-chat", timeout=30_000)
                assert page.locator("#page-chat").count() == 1
                page.click('[data-page="dashboard"]')
                page.click('[data-dashboard-tab="updates"]')
                assert page.locator("#updates-summary").count() == 1
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


def _run_docker_ui_assertions(url: str) -> None:
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                if page.locator("#onboarding-overlay").count():
                    overlay_text = page.locator("#onboarding-overlay").inner_text(timeout=5_000)
                    if "Ouroboros" in overlay_text:
                        return
                page.wait_for_selector("#page-chat", timeout=30_000)
                assert page.locator("#page-chat").count() == 1
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.fixture()
def direct_server_with_data(tmp_path):
    if os.environ.get("OUROBOROS_RUN_UI_SMOKE") != "1":
        pytest.skip("set OUROBOROS_RUN_UI_SMOKE=1 to run browser UI smoke")
    with MockLLMServer() as llm:
        port = _free_port()
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        model = "openai-compatible::mock-model"
        (data_dir / "settings.json").write_text(
            json.dumps(
                {
                    "OPENAI_COMPATIBLE_API_KEY": "ui-smoke-key",
                    "OPENAI_COMPATIBLE_BASE_URL": llm.base_url,
                    "OUROBOROS_MODEL": model,
                    "OUROBOROS_MODEL_CODE": model,
                    "OUROBOROS_MODEL_LIGHT": model,
                    "OUROBOROS_MODEL_FALLBACK": model,
                    "OUROBOROS_RUNTIME_MODE": "light",
                }
            ),
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "OUROBOROS_APP_ROOT": str(tmp_path),
            "OUROBOROS_DATA_DIR": str(data_dir),
            "OUROBOROS_REPO_DIR": REPO_ROOT,
            "OUROBOROS_SERVER_HOST": "127.0.0.1",
            "OUROBOROS_SERVER_PORT": str(port),
            "OUROBOROS_HOST_SERVICE_PORT": str(port + 1),
            "OUROBOROS_NETWORK_PASSWORD": "ui-smoke-password",
        }
        proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        url = f"http://127.0.0.1:{port}"
        try:
            _wait_health(url)
            yield {"url": url, "data_dir": data_dir}
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture()
def direct_server(direct_server_with_data):
    return direct_server_with_data["url"]


@pytest.mark.ui_browser
def test_ui_smoke_direct_mode_loads_chat_and_dashboard(direct_server):
    _run_core_ui_assertions(direct_server)


@pytest.mark.ui_browser
def test_ui_smoke_direct_mode_creates_task_with_mock_provider(direct_server):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(direct_server, wait_until="domcontentloaded", timeout=30_000)
                page.fill("#chat-input", "Respond with exactly OK")
                page.click("#chat-send")
                page.wait_for_selector(".chat-bubble.assistant", timeout=60_000)
                assert "OK" in page.locator("#chat-messages").inner_text(timeout=5_000)
                metrics = page.evaluate(
                    """() => {
                        const messages = document.querySelector('#chat-messages');
                        const remaining = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
                        return {
                            scrollTop: messages.scrollTop,
                            scrollHeight: messages.scrollHeight,
                            clientHeight: messages.clientHeight,
                            remaining,
                        };
                    }"""
                )
                assert metrics["remaining"] <= 4, metrics
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_ui_smoke_direct_mode_nests_subagent_child_cards(direct_server_with_data):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": "2026-05-25T10:00:00+00:00",
            "chat_id": 1,
            "task_id": "parent1",
            "content": "Parent task started",
            "is_progress": True,
        },
        {
            "ts": "2026-05-25T10:00:01+00:00",
            "chat_id": 1,
            "task_id": "child1",
            "content": "Scheduled subagent child1",
            "is_progress": True,
            "delegation_role": "subagent",
            "subagent_event": "scheduled",
            "subagent_task_id": "child1",
            "parent_task_id": "parent1",
            "root_task_id": "parent1",
            "subagent_role": "researcher",
        },
        {
            "ts": "2026-05-25T10:00:02+00:00",
            "chat_id": 1,
            "task_id": "child1",
            "content": "Subagent child1 running",
            "is_progress": True,
            "delegation_role": "subagent",
            "subagent_event": "running",
            "subagent_task_id": "child1",
            "parent_task_id": "parent1",
            "root_task_id": "parent1",
            "subagent_role": "researcher",
            "status": "running",
        },
        {
            "ts": "2026-05-25T10:00:02.500000+00:00",
            "chat_id": 1,
            "task_id": "child1",
            "content": "Searching evidence",
            "is_progress": True,
            "delegation_role": "subagent",
            "subagent_event": "progress",
            "subagent_task_id": "child1",
            "parent_task_id": "parent1",
            "root_task_id": "parent1",
            "subagent_role": "researcher",
            "status": "running",
        },
        {
            "ts": "2026-05-25T10:00:03+00:00",
            "chat_id": 1,
            "task_id": "child1",
            "content": "Subagent child1 completed",
            "is_progress": True,
            "delegation_role": "subagent",
            "subagent_event": "completed",
            "subagent_task_id": "child1",
            "parent_task_id": "parent1",
            "root_task_id": "parent1",
            "subagent_role": "researcher",
            "status": "completed",
            "cost_usd": 0.125,
            "result": "Child result with evidence table\n| source | verdict |\n| A | pass |",
            "trace_summary": "searched sources\ncompared output",
        },
    ]
    (logs_dir / "progress.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (logs_dir / "chat.jsonl").write_text(
        json.dumps({
            "ts": "2026-05-25T10:00:03.500000+00:00",
            "chat_id": 1,
            "direction": "out",
            "task_id": "child1",
            "text": "Final child answer should stay inside the child card.",
            "format": "markdown",
        }) + "\n",
        encoding="utf-8",
    )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector(".chat-live-card", timeout=30_000)
                # Subagents render as always-visible child cards nested under
                # the parent card. Child completion must not finish the parent.
                page.wait_for_function("() => document.querySelectorAll('.chat-live-card').length === 2", timeout=30_000)
                page.wait_for_function(
                    "() => { const p = document.querySelector('.chat-live-card:not(.subagent)');"
                    " const c = document.querySelector('.chat-live-card.subagent');"
                    " return !!p && !!c && c.closest('.chat-subagents') && c.parentElement.closest('.chat-live-card') === p"
                    " && /Subagent child1/.test(c.innerText) && /role=researcher/.test(c.innerText); }",
                    timeout=30_000,
                )
                parent = page.locator(".chat-live-card:not(.subagent)").first
                child = page.locator(".chat-live-card.subagent").first
                parent_text = parent.inner_text()
                child_text = child.inner_text()
                assert "Parent task started" in parent_text
                assert "Subagent child1" in child_text
                assert "child=child1" in child_text
                assert "role=researcher" in child_text
                assert parent.get_attribute("data-finished") == "0"
                assert child.get_attribute("data-finished") == "1"
                assert page.locator(".chat-bubble.progress").count() == 0
                assert page.locator(".chat-bubble").filter(
                    has_text="Final child answer should stay inside the child card."
                ).count() == 0

                # Expand only the child card + its lifecycle line to read the handoff.
                child.locator("[data-live-summary-button]").first.click()
                line_toggles = child.locator(".chat-live-line-toggle")
                if line_toggles.count():
                    line_toggles.last.click()
                expanded_text = child.inner_text(timeout=5_000)
                assert "Final child answer should stay inside the child card." in expanded_text
                assert "Child result with evidence table" in expanded_text
                assert "| source | verdict |" in expanded_text
                assert "searched sources" in expanded_text
                assert "compared output" in expanded_text
                assert "done" in expanded_text.lower()
                assert "Scheduled subagent child1" not in expanded_text
                assert child.locator("[data-live-summary-button]").first.get_attribute("aria-expanded") == "true"
                assert child.locator("[data-live-timeline]").first.get_attribute("id")
                assert child.locator(".chat-live-line-toggle").last.get_attribute("aria-controls")

                page.reload(wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function("() => document.querySelectorAll('.chat-live-card').length === 2", timeout=30_000)
                page.wait_for_function(
                    "() => { const p = document.querySelector('.chat-live-card:not(.subagent)');"
                    " const c = document.querySelector('.chat-live-card.subagent');"
                    " return !!p && !!c && c.closest('.chat-subagents') && c.parentElement.closest('.chat-live-card') === p; }",
                    timeout=30_000,
                )
                replay_parent = page.locator(".chat-live-card:not(.subagent)").first
                replay_child = page.locator(".chat-live-card.subagent").first
                assert replay_parent.get_attribute("data-finished") == "0"
                assert replay_child.get_attribute("data-finished") == "1"
                assert "Subagent child1" in replay_child.inner_text()
                assert "child=child1" in replay_child.inner_text()
                replay_child.locator("[data-live-summary-button]").first.click()
                assert "Final child answer should stay inside the child card." in replay_child.inner_text()
                assert page.locator(".chat-bubble.progress").count() == 0
                assert page.locator(".chat-bubble", has_text="Final child answer should stay inside the child card.").count() == 0
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
def test_ui_smoke_direct_mode_chat_scrolls_on_desktop(direct_server):
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    def scroll_metrics(page):
        return page.evaluate(
            """() => {
                const messages = document.querySelector('#chat-messages');
                if (!messages) return null;
                messages.scrollTop = 0;
                const top = messages.scrollTop;
                messages.scrollTop = messages.scrollHeight;
                const bottom = messages.scrollTop;
                return {
                    clientHeight: messages.clientHeight,
                    scrollHeight: messages.scrollHeight,
                    top,
                    bottom,
                    overflowY: getComputedStyle(messages).overflowY,
                    runtimeVvh: document.getElementById('runtime-vvh')?.textContent || '',
                    bodyHeight: Math.round(document.body.getBoundingClientRect().height),
                    windowHeight: window.innerHeight,
                };
            }"""
        )

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(direct_server, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_selector("#chat-messages", timeout=30_000)
                page.evaluate(
                    """() => {
                        const messages = document.querySelector('#chat-messages');
                        messages.replaceChildren();
                        for (let i = 0; i < 48; i += 1) {
                            const bubble = document.createElement('div');
                            bubble.className = 'chat-bubble assistant';
                            bubble.textContent = `Desktop scroll probe ${i} `.repeat(16);
                            bubble.style.minHeight = '48px';
                            messages.appendChild(bubble);
                        }
                    }"""
                )

                metrics = scroll_metrics(page)
                assert metrics is not None
                assert metrics["overflowY"] in {"auto", "scroll"}
                assert metrics["scrollHeight"] > metrics["clientHeight"] + 100
                assert metrics["bottom"] > metrics["top"] + 100
                assert "--vvh:100dvh" in metrics["runtimeVvh"]
                assert abs(metrics["bodyHeight"] - metrics["windowHeight"]) <= 2

                page.set_viewport_size({"width": 1280, "height": 400})
                page.wait_for_timeout(100)
                page.set_viewport_size({"width": 1280, "height": 800})
                page.wait_for_timeout(100)

                metrics_after_resize = scroll_metrics(page)
                assert metrics_after_resize is not None
                assert metrics_after_resize["scrollHeight"] > metrics_after_resize["clientHeight"] + 100
                assert metrics_after_resize["bottom"] > metrics_after_resize["top"] + 100
                assert "--vvh:100dvh" in metrics_after_resize["runtimeVvh"]
                assert abs(metrics_after_resize["bodyHeight"] - metrics_after_resize["windowHeight"]) <= 2
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser_docker
def test_ui_smoke_docker_mode_loads_health():
    if os.environ.get("OUROBOROS_RUN_DOCKER_UI_SMOKE") != "1":
        pytest.skip("set OUROBOROS_RUN_DOCKER_UI_SMOKE=1 to run Docker UI smoke")
    image = os.environ.get("OUROBOROS_DOCKER_UI_IMAGE", "ouroboros-web:test")
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20)
    if probe.returncode != 0:
        pytest.skip(f"Docker image missing: {image}")
    port = _free_port()
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", f"{port}:8765", image],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if run.returncode != 0:
        pytest.skip(f"Docker daemon unavailable or container failed: {run.stderr}")
    cid = run.stdout.strip()
    try:
        url = f"http://127.0.0.1:{port}"
        _wait_health(url, timeout_sec=45)
        _run_docker_ui_assertions(url)
    finally:
        subprocess.run(["docker", "stop", cid], capture_output=True, text=True, timeout=30)
