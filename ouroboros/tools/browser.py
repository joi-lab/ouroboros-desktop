"""
Browser automation tools via Playwright (sync API).

Provides browse_page (open URL, get content/screenshot)
and browser_action (click, fill, evaluate JS on current page).

Browser state lives in ToolContext (per-task lifecycle),
not module-level globals — safe across threads.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
import threading
from typing import Any, Dict, List

try:
    from playwright_stealth import Stealth
    _HAS_STEALTH = True
except ImportError:
    _HAS_STEALTH = False

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_playwright_ready = False
# Module-level Playwright instance to avoid greenlet threading issues
# Persists across ToolContext recreations but can be reset on error
_pw_instance = None
_pw_thread_id = None  # Track which thread owns the Playwright instance


def _ensure_playwright_installed():
    """Install Playwright and Chromium if not already available."""
    global _playwright_ready
    if _playwright_ready:
        return

    try:
        import playwright  # noqa: F401
    except ImportError:
        if getattr(sys, 'frozen', False):
            raise RuntimeError(
                "Browser tools require Playwright, which is not bundled. "
                "Install manually: pip3 install playwright && python3 -m playwright install chromium"
            )
        log.info("Playwright not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.executable_path
        log.info("Playwright chromium binary found")
    except Exception:
        if getattr(sys, 'frozen', False):
            raise RuntimeError(
                "Playwright chromium binary not found. "
                "Install manually: python3 -m playwright install chromium"
            )
        log.info("Installing Playwright chromium binary...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install-deps", "chromium"])

    _playwright_ready = True


def _reset_playwright_greenlet():
    """
    Fully reset Playwright's greenlet state by purging all related modules.
    This is necessary because sync_playwright() uses greenlets internally,
    and once a greenlet dies, it cannot be reused across "threads".
    """
    global _pw_instance, _pw_thread_id

    log.info("Resetting Playwright greenlet state...")

    # Kill any lingering chromium processes
    try:
        subprocess.run(["pkill", "-9", "-f", "chromium"], capture_output=True, timeout=5)
    except Exception:
        log.debug("Failed to kill chromium processes during reset", exc_info=True)
        pass

    # Purge all playwright modules from sys.modules to reset greenlet state
    mods_to_remove = [k for k in sys.modules.keys() if k.startswith('playwright')]
    for k in mods_to_remove:
        del sys.modules[k]

    # Also purge greenlet-related modules to ensure clean state
    mods_to_remove = [k for k in sys.modules.keys() if 'greenlet' in k.lower()]
    for k in mods_to_remove:
        try:
            del sys.modules[k]
        except Exception:
            log.debug(f"Failed to delete greenlet module {k} during reset", exc_info=True)
            pass

    # Reset module-level instance and thread ID
    _pw_instance = None
    _pw_thread_id = None
    log.info("Playwright greenlet state reset complete")


def _ensure_browser(ctx: ToolContext):
    """Create or reuse browser for this task. Browser state lives in ctx,
    but Playwright instance is module-level to avoid greenlet issues."""
    global _pw_instance, _pw_thread_id

    # Check if we've switched threads - if so, reset everything
    current_thread_id = threading.get_ident()
    if _pw_instance is not None and _pw_thread_id != current_thread_id:
        log.info(f"Thread switch detected (old={_pw_thread_id}, new={current_thread_id}). Resetting Playwright...")
        _reset_playwright_greenlet()

    if ctx.browser_state.browser is not None:
        try:
            if ctx.browser_state.browser.is_connected():
                return ctx.browser_state.page
        except Exception:
            log.debug("Browser connection check failed in _ensure_browser", exc_info=True)
            pass
        # Browser died — clean up and recreate
        cleanup_browser(ctx)

    _ensure_playwright_installed()

    # Use module-level Playwright instance to avoid greenlet threading issues
    if _pw_instance is None:
        from playwright.sync_api import sync_playwright

        try:
            _pw_instance = sync_playwright().start()
            _pw_thread_id = current_thread_id  # Record which thread owns this instance
            log.info(f"Created Playwright instance in thread {_pw_thread_id}")
        except RuntimeError as e:
            if "cannot switch" in str(e) or "different thread" in str(e):
                # Greenlet is dead, do a full reset
                _reset_playwright_greenlet()
                # Now import fresh and try again
                from playwright.sync_api import sync_playwright
                _pw_instance = sync_playwright().start()
                _pw_thread_id = current_thread_id
                log.info(f"Recreated Playwright instance in thread {_pw_thread_id} after error")
            else:
                raise

    # Store reference in ctx for cleanup
    ctx.browser_state.pw_instance = _pw_instance

    ctx.browser_state.browser = _pw_instance.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=site-per-process",
            "--window-size=1920,1080",
        ],
    )
    ctx.browser_state.page = ctx.browser_state.browser.new_page(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    )

    if _HAS_STEALTH:
        stealth = Stealth()
        stealth.apply_stealth_sync(ctx.browser_state.page)

    ctx.browser_state.page.set_default_timeout(30000)
    return ctx.browser_state.page


def cleanup_browser(ctx: ToolContext) -> None:
    """Close browser and playwright. Called by agent.py in finally block.

    Note: We DON'T stop the module-level _pw_instance here to allow reuse
    across tasks. Only close the browser and page for this context.
    """
    global _pw_instance

    try:
        if ctx.browser_state.page is not None:
            ctx.browser_state.page.close()
    except Exception:
        log.debug("Failed to close browser page during cleanup", exc_info=True)
        pass
    try:
        if ctx.browser_state.browser is not None:
            ctx.browser_state.browser.close()
    except Exception as e:
        # If browser cleanup fails with thread error, reset everything
        if "cannot switch" in str(e) or "different thread" in str(e):
            log.warning("Browser cleanup hit thread error, resetting Playwright...")
            _reset_playwright_greenlet()

    # Clear ctx references but keep module-level _pw_instance alive for reuse
    ctx.browser_state.page = None
    ctx.browser_state.browser = None
    ctx.browser_state.pw_instance = None


_MARKDOWN_JS = """() => {
    const walk = (el) => {
        let out = '';
        for (const child of el.childNodes) {
            if (child.nodeType === 3) {
                const t = child.textContent.trim();
                if (t) out += t + ' ';
            } else if (child.nodeType === 1) {
                const tag = child.tagName;
                if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                if (['H1','H2','H3','H4','H5','H6'].includes(tag))
                    out += '\\n' + '#'.repeat(parseInt(tag[1])) + ' ';
                if (tag === 'P' || tag === 'DIV' || tag === 'BR') out += '\\n';
                if (tag === 'LI') out += '\\n- ';
                if (tag === 'A') out += '[';
                out += walk(child);
                if (tag === 'A') out += '](' + (child.href||'') + ')';
            }
        }
        return out;
    };
    return walk(document.body);
}"""


def _extract_page_output(page: Any, output: str, ctx: ToolContext) -> str:
    """Extract page content in the requested format."""
    if output == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        ctx.browser_state.last_screenshot_b64 = b64
        return (
            f"Screenshot captured ({len(b64)} bytes base64). "
            f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
        )
    elif output == "html":
        html = page.content()
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    elif output == "markdown":
        text = page.evaluate(_MARKDOWN_JS)
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")
    else:  # text
        text = page.inner_text("body")
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")


def _browse_page(ctx: ToolContext, url: str, output: str = "text",
                 wait_for: str = "", timeout: int = 30000) -> str:
    try:
        page = _ensure_browser(ctx)
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        if wait_for:
            page.wait_for_selector(wait_for, timeout=timeout)
        return _extract_page_output(page, output, ctx)
    except Exception as e:
        if "cannot switch" in str(e) or "different thread" in str(e) or "greenlet" in str(e).lower():
            log.warning(f"Browser thread error detected: {e}. Resetting Playwright and retrying...")
            cleanup_browser(ctx)
            _reset_playwright_greenlet()
            page = _ensure_browser(ctx)
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            if wait_for:
                page.wait_for_selector(wait_for, timeout=timeout)
            return _extract_page_output(page, output, ctx)
        raise


def _browser_action(ctx: ToolContext, action: str, selector: str = "",
                    value: str = "", timeout: int = 5000) -> str:
    def _do_action():
        page = _ensure_browser(ctx)

        if action == "click":
            if not selector:
                return "Error: selector required for click"
            page.click(selector, timeout=timeout)
            page.wait_for_timeout(500)
            return f"Clicked: {selector}"
        elif action == "fill":
            if not selector:
                return "Error: selector required for fill"
            page.fill(selector, value, timeout=timeout)
            return f"Filled {selector} with: {value}"
        elif action == "select":
            if not selector:
                return "Error: selector required for select"
            page.select_option(selector, value, timeout=timeout)
            return f"Selected {value} in {selector}"
        elif action == "screenshot":
            data = page.screenshot(type="png", full_page=False)
            b64 = base64.b64encode(data).decode()
            ctx.browser_state.last_screenshot_b64 = b64
            return (
                f"Screenshot captured ({len(b64)} bytes base64). "
                f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
            )
        elif action == "evaluate":
            if not value:
                return "Error: value (JS code) required for evaluate"
            result = page.evaluate(value)
            out = str(result)
            return out[:20000] + ("... [truncated]" if len(out) > 20000 else "")
        elif action == "scroll":
            direction = value or "down"
            if direction == "down":
                page.evaluate("window.scrollBy(0, 600)")
            elif direction == "up":
                page.evaluate("window.scrollBy(0, -600)")
            elif direction == "top":
                page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return f"Scrolled {direction}"
        else:
            return f"Unknown action: {action}. Use: click, fill, select, screenshot, evaluate, scroll"

    try:
        return _do_action()
    except (RuntimeError, Exception) as e:
        # Catch greenlet threading errors and reset Playwright completely
        if "cannot switch" in str(e) or "different thread" in str(e) or "greenlet" in str(e).lower():
            log.warning(f"Browser thread error detected: {e}. Resetting Playwright and retrying...")
            cleanup_browser(ctx)
            _reset_playwright_greenlet()
            # Retry once with fresh state
            return _do_action()
        else:
            raise


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": (
                    "Open a URL in headless browser. Returns page content as text, "
                    "html, markdown, or screenshot (base64 PNG). "
                    "Browser persists across calls within a task. "
                    "For screenshots: use send_photo tool to deliver the image to owner."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to open"},
                        "output": {
                            "type": "string",
                            "enum": ["text", "html", "markdown", "screenshot"],
                            "description": "Output format (default: text)",
                        },
                        "wait_for": {
                            "type": "string",
                            "description": "CSS selector to wait for before extraction",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Page load timeout in ms (default: 30000)",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=_browse_page,
            timeout_sec=60,
        ),
        ToolEntry(
            name="browser_action",
            schema={
                "name": "browser_action",
                "description": (
                    "Perform action on current browser page. Actions: "
                    "click (selector), fill (selector + value), select (selector + value), "
                    "screenshot (base64 PNG), evaluate (JS code in value), "
                    "scroll (value: up/down/top/bottom)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "fill", "select", "screenshot", "evaluate", "scroll"],
                            "description": "Action to perform",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for click/fill/select",
                        },
                        "value": {
                            "type": "string",
                            "description": "Value for fill/select, JS for evaluate, direction for scroll",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Action timeout in ms (default: 5000)",
                        },
                    },
                    "required": ["action"],
                },
            },
            handler=_browser_action,
            timeout_sec=60,
        ),
    ]
