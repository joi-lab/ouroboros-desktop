"""Place: who is in front, bringing an application forward, window rows, bounded element reads.

Mixed into the plugin's ``_ComputerUse``. The plugin supplies the seams (``_plat``, ``_session``,
``_which_tool``, ``_json_text``, ``_try``, ``_helper_python``, ``_backend_unavailable``,
``_disabled_connection_error``, ``_is_remote``, ``_active_connection``) and the remote mixin the
remote readers (``_remote_activate``, ``_remote_window_list``, ``_remote_ax_tree``).

Contract (unix_computer_use SKILL.md, "Input receipts"): an input aimed at an application is sent
only after that application was brought forward and read as frontmost; the receipt states who was
in front when the input was dispatched (``delivered_to``), who was there before (``front_before``,
when it differs), who is there afterwards (``front_after``, when it changed), and what the tool did
not observe. macOS facts come from a short-lived helper process (``macos_native.py``); X11 facts
from ``wmctrl``/``xdotool``; Wayland is an honest refusal.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
from typing import Any

from .macos_native import parse_info


class PlaceMixin:
    """Target resolution, activation, place receipts, window rows and bounded element reads."""

    def _native(self, op: str, *args: Any, timeout: int = 20) -> dict[str, Any]:
        """One fresh macOS native fact from the helper process (``macos_native.py``).

        A separate short-lived process so that a stuck or crashing native call can be killed
        and never takes the host down, and so that every answer is read anew (AppKit caches
        refresh only on a main run loop the server does not turn)."""
        cmd = [self._helper_python(), "-I", str(pathlib.Path(__file__).with_name("macos_native.py")), op, *[str(a) for a in args]]
        rc, out, err = self._try(cmd, timeout=timeout)
        for line in reversed(out.strip().splitlines()):
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                except ValueError:
                    break
                if isinstance(parsed, dict):
                    return parsed
                break
        return {"ok": False, "error": f"native helper gave no answer ({(err or out).strip()[:200] or f'exit {rc}'})", "op": op}

    def _front_local(self) -> dict[str, Any] | None:
        """Who is in front NOW, read fresh each call: macOS via ``lsappinfo``, X11 via ``xdotool``; None = unknown."""
        plat = self._plat()
        if plat == "macos" and self._which_tool("lsappinfo"):
            rc, out, _err = self._try(["lsappinfo", "front"], timeout=5)
            asn = out.strip()
            if rc == 0 and asn:
                rc, out, _err = self._try(["lsappinfo", "info", "-only", "pid", "-only", "bundleid", "-only", "name", asn], timeout=5)
                info = parse_info(out) if rc == 0 else {}
                if info.get("pid"):
                    return info
            return None
        if plat == "linux" and self._session() != "wayland" and self._which_tool("xdotool"):
            rc, out, _err = self._try(["xdotool", "getactivewindow", "getwindowpid", "getwindowclassname", "getwindowname"], timeout=5)
            lines = [ln.strip() for ln in out.splitlines()]
            if rc == 0 and lines and lines[0].isdigit():
                front: dict[str, Any] = {"pid": int(lines[0])}
                if len(lines) > 1:
                    front["app"] = lines[1]
                if len(lines) > 2:
                    front["title"] = lines[2]
                return front
        return None

    def _x11_windows(self) -> tuple[list[dict[str, Any]], str]:
        """``wmctrl -lpxG`` rows: window_id, desktop, pid, bounds, app (WM_CLASS), title."""
        if not self._which_tool("wmctrl"):
            return [], "wmctrl is not installed"
        rc, out, err = self._try(["wmctrl", "-lpxG"], timeout=10)
        if rc != 0:
            return [], err.strip() or out.strip() or f"wmctrl exit {rc}"
        rows: list[dict[str, Any]] = []
        for line in out.splitlines():
            parts = line.split(None, 9)
            if len(parts) < 9:
                continue
            try:
                rows.append({"window_id": parts[0], "desktop": int(parts[1]), "pid": int(parts[2]),
                             "bounds": [int(parts[3]), int(parts[4]), int(parts[5]), int(parts[6])],
                             "app": parts[7], "title": parts[9] if len(parts) > 9 else ""})
            except ValueError:
                continue
        return rows, ""

    def _x11_activate(self, query: str, wait_ms: int) -> dict[str, Any]:
        rows, err = self._x11_windows()
        if err:
            return {"ok": False, "reason": "unsupported", "error": err}
        q = str(query or "").strip().lower()
        if q.startswith("pid:") and q[4:].isdigit():
            matches = [r for r in rows if r["pid"] == int(q[4:])]
        elif q.startswith("0x"):
            matches = [r for r in rows if r["window_id"].lower() == q]
        else:
            matches = [r for r in rows if q in r["app"].lower() or q in r["title"].lower()]
            exact = [r for r in matches if r["app"].lower().rsplit(".", 1)[-1] == q or r["title"].lower() == q]
            matches = exact or matches
        if not matches:
            return {"ok": False, "reason": "not_running", "running": sorted({r["app"] for r in rows})[:40]}
        if len({r["pid"] for r in matches}) > 1:
            return {"ok": False, "reason": "ambiguous",
                    "candidates": [{"app": r["app"], "pid": r["pid"], "window_id": r["window_id"], "title": r["title"]} for r in matches[:10]]}
        target = {"app": matches[0]["app"], "pid": matches[0]["pid"], "window_id": matches[0]["window_id"], "title": matches[0]["title"]}
        before = self._front_local()
        if before and before.get("pid") == target["pid"]:
            return {"ok": True, "target": target, "front_before": before, "front": before, "activated": False, "already_front": True, "waited_ms": 0}
        rc, _out, err2 = self._try(["wmctrl", "-i", "-a", target["window_id"]], timeout=10)
        if rc != 0:
            return {"ok": False, "reason": "activate_failed", "error": err2.strip() or f"wmctrl exit {rc}", "target": target, "front_before": before}
        t0 = time.monotonic()
        front = self._front_local()
        while front is not None and front.get("pid") != target["pid"] and (time.monotonic() - t0) * 1000 < max(200, int(wait_ms)):
            time.sleep(0.05)
            front = self._front_local()
        waited = int((time.monotonic() - t0) * 1000)
        out: dict[str, Any] = {"ok": True, "target": target, "front_before": before, "front": front, "method": "wmctrl -i -a", "waited_ms": waited}
        if front is None:
            out["activated"] = None  # no xdotool: the switch could not be verified
        elif front.get("pid") == target["pid"]:
            out["activated"] = True
        else:
            out.update({"ok": False, "activated": False, "reason": "not_frontmost_after_activate"})
        return out

    @staticmethod
    def _target_error(app: str, res: dict[str, Any]) -> str:
        reason = str(res.get("reason") or "")
        front = res.get("front") or {}
        where = f" ({front.get('app')} is in front)" if isinstance(front, dict) and front.get("app") else ""
        if reason == "not_running":
            return f"{app!r} is not a running application; nothing was sent (running: {', '.join(res.get('running') or [])[:300]})"
        if reason == "ambiguous":
            names = ", ".join(f"{c.get('app')} pid:{c.get('pid')}" for c in (res.get("candidates") or [])[:6])
            return f"{app!r} matches several applications ({names}); name one, e.g. pid:<n>; nothing was sent"
        if reason == "not_frontmost_after_activate":
            return (f"{app!r} did not come to the front{where}; nothing was sent. The OS can refuse activation "
                    "from a background process: ask the owner to click it, or use the app's own commands (run_command)")
        if reason == "unsupported":
            return f"application targeting is unavailable here: {res.get('error') or 'no window manager access'}; nothing was sent"
        return str(res.get("error") or f"{app!r} could not be targeted{where}; nothing was sent")

    def _begin(self, app: str, wait_ms: int = 2000) -> tuple[str | None, dict[str, Any]]:
        """Before an input: resolve and bring ``app`` forward (launch-free) and verify it is in front.

        Returns ``(refusal_json, context)``: a refusal sends nothing. With no ``app`` the context only
        records who is in front now, which is who will receive the input."""
        app = str(app or "").strip()
        ctx: dict[str, Any] = {}
        if not app:
            ctx["front_before"] = self._front_local()
            return None, ctx
        plat = self._plat()
        if plat == "macos":
            res = self._native("activate", app, int(wait_ms), timeout=max(12, int(wait_ms) // 1000 + 10))
        elif plat == "linux" and self._session() != "wayland":
            res = self._x11_activate(app, int(wait_ms))
        else:
            res = {"ok": False, "reason": "unsupported", "error": f"no application targeting on {plat}/{self._session()}"}
        ctx["target"] = {"requested": app, "resolved": res.get("target"), "activated": res.get("activated")}
        if res.get("method"):
            ctx["target"]["method"] = res["method"]
        if res.get("waited_ms") is not None:
            ctx["target"]["waited_ms"] = res["waited_ms"]
        ctx["front_before"] = res.get("front_before")
        if not res.get("ok"):
            refusal: dict[str, Any] = {"ok": False, "reason": str(res.get("reason") or "target_unavailable"),
                                       "error": self._target_error(app, res), "target": ctx["target"]}
            if res.get("candidates"):
                refusal["candidates"] = res["candidates"]
            if res.get("running"):
                refusal["running"] = res["running"]
            if res.get("front") is not None:
                refusal["front"] = res["front"]
            if res.get("activated"):
                # The switch happened (an observed external effect) even though the input is withheld.
                refusal["observed_effect"] = True
                refusal["activation_done"] = True
            return self._json_text(refusal), ctx
        ctx["delivered_to"] = res.get("front") or res.get("target")
        return None, ctx

    def _finish(self, payload: dict[str, Any], ctx: dict[str, Any], stderr: str = "") -> dict[str, Any]:
        """Add the place facts to an input receipt, and demote a dispatch macOS dropped (cliclick warns on stderr)."""
        if ctx.get("target"):
            payload["target"] = ctx["target"]
        delivered = ctx.get("delivered_to") or ctx.get("front_before")
        payload["delivered_to"] = delivered if isinstance(delivered, dict) else "unknown"
        before = ctx.get("front_before")
        if isinstance(before, dict) and isinstance(delivered, dict) and before.get("pid") != delivered.get("pid"):
            payload["front_before"] = before
        after = self._front_local()
        if after is not None and (not isinstance(delivered, dict) or after.get("pid") != delivered.get("pid")):
            payload["front_after"] = after
        text = stderr.strip()
        if text:
            payload["helper_stderr"] = text[:300]
        dropped = "accessibility" in text.lower() and "not enabled" in text.lower()
        untyped = re.findall(r"Unable to get key code for (\S+)", text)
        if untyped and payload.get("ok"):
            # cliclick types through key codes of the ACTIVE keyboard layout and exits 0 when it cannot find one
            # (measured 27.10.2026: Latin text under a Cyrillic layout typed nothing). Partial typing is unknown.
            payload.pop("observed_effect", None)
            payload.update({"ok": False, "reason": "untypable_characters", "untyped": untyped[:20],
                            "error": f"{len(untyped)} character(s) have no key code in the active keyboard layout and were "
                                     "not typed (the rest may have been); use type_text with via=paste"})
        elif dropped and payload.get("ok"):
            payload.pop("observed_effect", None)
            payload.update({"ok": False, "reason": "permission_denied",
                            "permission": {"accessibility": "untrusted", "source": "helper stderr"},
                            "error": "macOS drops synthetic input from a process without the Accessibility grant: "
                                     "the helper exited 0 but nothing reached the screen"})
        elif payload.get("observed_effect"):
            where = delivered.get("app") if isinstance(delivered, dict) else None
            payload["note"] = ((f"dispatched while {where} was in front" if where else "dispatched; who was in front is unknown")
                               + "; this tool observed no UI change: verify with ax_tree, a screenshot or the app's own commands")
        return payload

    def activate_app(self, *, app: str, wait_ms: int = 2000) -> str:
        """Bring a running application to the front and report who is in front afterwards (never launches)."""
        app = str(app or "").strip()
        if not app:
            return self._json_text({"ok": False, "error": "app is required (a running application's name, bundle id or pid:<n>)"})
        wait_ms = max(200, min(10_000, int(wait_ms or 2000)))
        if self._is_remote():
            _name, conn = self._active_connection()
            if conn.get("disabled"):
                return self._disabled_connection_error(_name, conn)
            return self._remote_activate(conn, app, wait_ms)
        refusal, ctx = self._begin(app, wait_ms)
        if refusal:
            return refusal
        target = ctx["target"]
        payload: dict[str, Any] = {"ok": True, "target": target, "front": ctx.get("delivered_to"), "activated": bool(target.get("activated"))}
        if ctx.get("front_before") is not None:
            payload["front_before"] = ctx["front_before"]
        if target.get("activated"):
            payload["observed_effect"] = True  # the frontmost application changed: an observed external effect
            payload["note"] = f"{app} is now in front; inputs without app= go there until the owner switches away"
        elif target.get("activated") is None:
            payload["note"] = "the switch was requested but could not be verified (no active-window reader)"
        else:
            payload["note"] = f"{app} was already in front; nothing changed"
        return self._json_text(payload)

    def window_list(self, *, app: str = "", limit: int = 50) -> str:
        """Running applications and their windows with the frontmost named (titles need the OS grant)."""
        plat = self._plat()
        limit = max(1, min(500, int(limit or 50)))
        query = str(app or "").strip().lower()
        if self._is_remote():
            _name, conn = self._active_connection()
            if conn.get("disabled"):
                return self._disabled_connection_error(_name, conn)
            return self._remote_window_list(conn, query, limit)
        if plat == "macos":
            res = self._native("apps", limit, timeout=25)
            if not res.get("ok"):
                return self._json_text({"ok": False, "platform": plat, "error": res.get("error") or "no answer", "frontmost": self._front_local()})
            rows = res.get("apps") or []
            if query:
                rows = [r for r in rows if query in (r.get("app") or "").lower() or query == (r.get("app_id") or "").lower()
                        or query == f"pid:{r.get('pid')}"]
            return self._json_text({"ok": True, "platform": plat, "frontmost": res.get("frontmost"), "windows": rows,
                          "titles_available": bool(res.get("titles_available")), "truncated": bool(res.get("truncated")),
                          "limit": limit, "note": "rows are applications with their windows; pass app=<name|bundle id|pid:n> "
                                                   "to the input tools to act in one of them"})
        if plat == "linux":
            if self._session() == "wayland" and not self._which_tool("wmctrl"):
                return self._json_text({"ok": False, "platform": plat, "session_type": "wayland", "reason": "unsupported",
                              "error": "this Wayland session exposes no window list to other programs "
                                       f"({os.environ.get('XDG_CURRENT_DESKTOP') or 'unknown desktop'}); ask the owner to bring the app forward"})
            rows, err = self._x11_windows()
            if err:
                return self._json_text({"ok": False, "platform": plat, "error": err})
            front = self._front_local()
            for row in rows:
                row["frontmost"] = bool(front and front.get("pid") == row["pid"])
            if query:
                rows = [r for r in rows if query in r["app"].lower() or query in r["title"].lower() or query == f"pid:{r['pid']}"]
            payload: dict[str, Any] = {"ok": True, "platform": plat, "session_type": self._session(), "frontmost": front,
                                       "windows": rows[:limit], "truncated": len(rows) > limit, "limit": limit}
            if self._session() == "wayland":
                payload["note"] = "XWayland windows only: native Wayland windows are invisible to wmctrl"
            return self._json_text(payload)
        return self._backend_unavailable("window listing")

    def ax_tree(self, *, app: str = "", max_nodes: int = 150, timeout_ms: int = 3000, root: str = "window",
                include_web: bool = False) -> str:
        """Bounded accessibility read of one window (the frontmost app's, or ``app``'s, without bringing it forward)."""
        plat = self._plat()
        app = str(app or "").strip()
        max_nodes = max(1, min(2000, int(max_nodes or 150)))
        timeout_ms = max(100, min(15_000, int(timeout_ms or 3000)))
        root = "focused" if str(root or "").lower() == "focused" else "window"
        if self._is_remote():
            _name, conn = self._active_connection()
            if conn.get("disabled"):
                return self._disabled_connection_error(_name, conn)
            return self._remote_ax_tree(conn)
        if plat == "macos":
            if app:
                res = self._native("resolve", app, timeout=15)
                if not res.get("ok"):
                    return self._json_text({"ok": False, "platform": plat, "reason": res.get("reason") or "unresolved",
                                  "error": self._target_error(app, res), "candidates": res.get("candidates") or []})
                target = res["candidates"][0]
            else:
                target = self._front_local()
                if not target:
                    return self._json_text({"ok": False, "platform": plat, "reason": "front_unknown", "error": "the frontmost application could not be read"})
            tree = self._native("tree", target["pid"], max_nodes, timeout_ms, root, "true" if include_web else "false",
                                timeout=timeout_ms // 1000 + 8)
            if not tree.get("ok"):
                return self._json_text({"ok": False, "platform": plat, "app": target, "reason": tree.get("reason") or "ax_unavailable",
                              "error": tree.get("error") or f"the accessibility read failed ({tree.get('reason')})"})
            tree.update({"platform": plat, "app": target, "coordinate_note": "node and mark coordinates are INPUT-space "
                         "(macOS logical points): click them with raw=true"})
            return self._json_text(tree)
        if plat == "linux":
            rows, err = self._x11_windows()
            return self._json_text({"ok": False, "platform": plat, "reason": "unsupported", "session_type": self._session(),
                          "error": "no element reader on Linux in this skill (use the app's own commands or screenshot)",
                          "windows": rows, **({"windows_error": err} if err else {})})
        return self._backend_unavailable("accessibility tree")

