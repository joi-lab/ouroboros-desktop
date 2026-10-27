"""macOS native facts for unix_computer_use, run as a short-lived helper process.

``python -I macos_native.py <op> [args...]`` prints one JSON object. The plugin starts it
through its ordinary process boundary (``_run``) so that a stuck or crashing native call
can be killed and can never take the host server down, and so that every answer is fresh
(AppKit caches such as ``NSWorkspace.frontmostApplication`` refresh only on a main run loop,
which the long-lived server does not turn). Pure ``ctypes`` against ApplicationServices and
CoreGraphics: no pyobjc wrapper is required (the packaged interpreter carries AppKit and
Quartz but not the Accessibility API, and source installs carry none).

Operations (all read-only except ``activate``):

- ``front``                       the frontmost application (``lsappinfo``) and its focused window title
- ``apps [limit]``                running regular applications with their windows, ``frontmost`` and ``scriptable``
- ``resolve <query>``             which running application a name, bundle id or ``pid:<n>`` means (0, 1 or many)
- ``activate <query> [wait_ms]``  resolve, bring forward if needed, re-read who is in front (never launches)
- ``tree <pid> [max_nodes] [timeout_ms] [root] [include_web]``  bounded accessibility walk of one window
- ``perms``                       prompt-free permission preflights (Accessibility, Screen Recording, input events)
- ``displays``                    active displays: bounds in global points and pixel sizes

Honesty rules: ``ok`` answers the question asked; ``unknown`` is spelled out, never rendered as an
empty list or ``false``; a window the walk did not finish says so in ``truncated_by``; ``-25204``
(the app did not answer in time) is busy, not denied.
"""

from __future__ import annotations

import ctypes
import json
import plistlib
import re
import subprocess
import sys
import time
from collections import deque
from ctypes import POINTER, Structure, byref, c_char_p, c_double, c_float, c_int32, c_long, c_size_t, c_ubyte, c_uint32, c_ulong, c_void_p, create_string_buffer
from typing import Any, Callable

_AS_PATH = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_UTF8 = 0x08000100
_LSAPPINFO = "/usr/bin/lsappinfo"
AX_ERR = {0: "ok", -25200: "Failure", -25201: "IllegalArgument", -25202: "InvalidUIElement", -25204: "CannotComplete",
          -25205: "AttributeUnsupported", -25206: "ActionUnsupported", -25208: "NotImplemented", -25211: "APIDisabled",
          -25212: "NoValue", -25213: "ParameterizedAttributeUnsupported", -25214: "NotEnoughPrecision"}
ACTIONABLE = {"AXButton", "AXTextField", "AXTextArea", "AXLink", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
              "AXComboBox", "AXMenuButton", "AXTab", "AXMenuItem", "AXRow", "AXCell", "AXStaticText", "AXImage"}
_WALK_ATTRS = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPosition", "AXSize"]
TREE_CEILINGS = {"max_nodes": 2000, "timeout_ms": 15000}


class CGPoint(Structure):
    _fields_ = [("x", c_double), ("y", c_double)]


class CGSize(Structure):
    _fields_ = [("w", c_double), ("h", c_double)]


class CGRect(Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


# ----------------------------------------------------------------------------------------------
# lsappinfo: fresh application facts without AppKit (format is Apple's CLI output, parsed loosely)
# ----------------------------------------------------------------------------------------------

def _sh(cmd: list[str], timeout: float = 5.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout
    except Exception:  # noqa: BLE001 - the caller renders an unknown
        return ""


def parse_info(text: str) -> dict[str, Any]:
    """``lsappinfo info -only pid -only bundleid -only name <asn>`` -> {pid, app_id, app}."""
    out: dict[str, Any] = {}
    m = re.search(r'"pid"\s*=\s*(\d+)', text)
    if m:
        out["pid"] = int(m.group(1))
    m = re.search(r'"CFBundleIdentifier"\s*=\s*"([^"]*)"', text)
    if m:
        out["app_id"] = m.group(1)
    m = re.search(r'"LSDisplayName"\s*=\s*"([^"]*)"', text)
    if m:
        out["app"] = m.group(1)
    return out


def front_app() -> dict[str, Any] | None:
    asn = _sh([_LSAPPINFO, "front"]).strip()
    if not asn:
        return None
    info = parse_info(_sh([_LSAPPINFO, "info", "-only", "pid", "-only", "bundleid", "-only", "name", asn]))
    return info if info.get("pid") else None


def parse_list(text: str) -> list[dict[str, Any]]:
    """``lsappinfo list`` -> regular (``type="Foreground"``) applications: {app, pid, app_id, path}."""
    apps: list[dict[str, Any]] = []
    for block in re.split(r"\n(?= *\d+\) )", text):
        head = re.match(r'\s*\d+\) "(.*)" ASN:', block)
        if not head or 'type="Foreground"' not in block:
            continue
        pid = re.search(r"\bpid\s*=\s*(\d+)", block)
        if not pid:
            continue
        bundle = re.search(r'bundleID="([^"]*)"', block)
        path = re.search(r'bundle path="([^"]*)"', block)
        apps.append({"app": head.group(1), "pid": int(pid.group(1)),
                     "app_id": bundle.group(1) if bundle else "", "path": path.group(1) if path else ""})
    return apps


def running_apps() -> list[dict[str, Any]]:
    return parse_list(_sh([_LSAPPINFO, "list"], timeout=8.0))


def scriptable(bundle_path: str) -> bool | None:
    """Whether the bundle declares an AppleScript dictionary (Info.plist); None when unreadable."""
    if not bundle_path:
        return None
    try:
        with open(bundle_path.rstrip("/") + "/Contents/Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except Exception:  # noqa: BLE001
        return None
    enabled = info.get("NSAppleScriptEnabled")
    return bool(info.get("OSAScriptingDefinition")) or enabled is True or str(enabled).upper() == "YES"


def resolve(query: str, apps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Launch-free target resolution: ``pid:<n>``, exact bundle id, or case-insensitive display/executable name."""
    q = str(query or "").strip()
    if not q:
        return []
    if q.lower().startswith("pid:") and q[4:].strip().isdigit():
        pid = int(q[4:].strip())
        return [a for a in apps if a["pid"] == pid]
    if q.isdigit():
        return [a for a in apps if a["pid"] == int(q)]
    exact_id = [a for a in apps if a["app_id"] and a["app_id"].lower() == q.lower()]
    if exact_id:
        return exact_id
    low = q.lower()
    matches = [a for a in apps if a["app"].lower() == low or a["path"].rstrip("/").rsplit("/", 1)[-1].lower() in (low, low + ".app")]
    return matches


# ----------------------------------------------------------------------------------------------
# Accessibility API through ctypes (pattern measured on 27.10.2026: 37 nodes in 1.6 s cold / 50 ms warm)
# ----------------------------------------------------------------------------------------------

class _AX:
    """Lazily bound ApplicationServices/CoreFoundation symbols; one instance per helper process."""

    def __init__(self) -> None:
        self.AS = ctypes.CDLL(_AS_PATH)
        self.CF = ctypes.CDLL(_CF_PATH)
        sig = self._sig
        self.CFRelease = sig(self.CF, "CFRelease", None, [c_void_p])
        self.CFRetain = sig(self.CF, "CFRetain", c_void_p, [c_void_p])
        self.CFGetTypeID = sig(self.CF, "CFGetTypeID", c_ulong, [c_void_p])
        self.T_STR = sig(self.CF, "CFStringGetTypeID", c_ulong, [])()
        self.T_ARR = sig(self.CF, "CFArrayGetTypeID", c_ulong, [])()
        self.T_NUM = sig(self.CF, "CFNumberGetTypeID", c_ulong, [])()
        self.T_BOOL = sig(self.CF, "CFBooleanGetTypeID", c_ulong, [])()
        self.CFStringCreateWithCString = sig(self.CF, "CFStringCreateWithCString", c_void_p, [c_void_p, c_char_p, c_uint32])
        self.CFStringGetLength = sig(self.CF, "CFStringGetLength", c_long, [c_void_p])
        self.CFStringGetMaximumSizeForEncoding = sig(self.CF, "CFStringGetMaximumSizeForEncoding", c_long, [c_long, c_uint32])
        self.CFStringGetCString = sig(self.CF, "CFStringGetCString", c_ubyte, [c_void_p, c_void_p, c_long, c_uint32])
        self.CFArrayGetCount = sig(self.CF, "CFArrayGetCount", c_long, [c_void_p])
        self.CFArrayGetValueAtIndex = sig(self.CF, "CFArrayGetValueAtIndex", c_void_p, [c_void_p, c_long])
        self.CFArrayCreate = sig(self.CF, "CFArrayCreate", c_void_p, [c_void_p, c_void_p, c_long, c_void_p])
        self.CFBooleanGetValue = sig(self.CF, "CFBooleanGetValue", c_ubyte, [c_void_p])
        self.CFNumberGetValue = sig(self.CF, "CFNumberGetValue", c_ubyte, [c_void_p, c_long, c_void_p])
        self.ARRAY_CB = ctypes.addressof(ctypes.c_byte.in_dll(self.CF, "kCFTypeArrayCallBacks"))
        self.kCFBooleanTrue = c_void_p.in_dll(self.CF, "kCFBooleanTrue").value
        self.AXIsProcessTrusted = sig(self.AS, "AXIsProcessTrusted", c_ubyte, [])
        self.AXUIElementCreateApplication = sig(self.AS, "AXUIElementCreateApplication", c_void_p, [c_int32])
        self.AXUIElementCopyAttributeValue = sig(self.AS, "AXUIElementCopyAttributeValue", c_int32, [c_void_p, c_void_p, POINTER(c_void_p)])
        self.AXUIElementSetAttributeValue = sig(self.AS, "AXUIElementSetAttributeValue", c_int32, [c_void_p, c_void_p, c_void_p])
        self.AXUIElementCopyMultipleAttributeValues = sig(self.AS, "AXUIElementCopyMultipleAttributeValues", c_int32,
                                                          [c_void_p, c_void_p, c_uint32, POINTER(c_void_p)])
        self.AXUIElementCopyAttributeValues = sig(self.AS, "AXUIElementCopyAttributeValues", c_int32,
                                                  [c_void_p, c_void_p, c_long, c_long, POINTER(c_void_p)])
        self.AXUIElementSetMessagingTimeout = sig(self.AS, "AXUIElementSetMessagingTimeout", c_int32, [c_void_p, c_float])
        self.AXValueGetType = sig(self.AS, "AXValueGetType", c_uint32, [c_void_p])
        self.AXValueGetValue = sig(self.AS, "AXValueGetValue", c_ubyte, [c_void_p, c_uint32, c_void_p])
        self.T_AXVALUE = sig(self.AS, "AXValueGetTypeID", c_ulong, [])()
        try:  # private but long-stable: AX window element -> CGWindowID
            self.AXUIElementGetWindow = sig(self.AS, "_AXUIElementGetWindow", c_int32, [c_void_p, POINTER(c_uint32)])
        except AttributeError:
            self.AXUIElementGetWindow = None
        self._strings: dict[str, int] = {}

    @staticmethod
    def _sig(lib, name, restype, argtypes):
        f = getattr(lib, name)
        f.restype, f.argtypes = restype, argtypes
        return f

    def cfs(self, name: str) -> int:
        ref = self._strings.get(name)
        if ref is None:
            ref = self.CFStringCreateWithCString(None, name.encode(), _UTF8)
            self._strings[name] = ref
        return ref

    def to_py(self, ref) -> Any:
        if not ref:
            return None
        tid = self.CFGetTypeID(ref)
        if tid == self.T_STR:
            n = self.CFStringGetLength(ref)
            size = self.CFStringGetMaximumSizeForEncoding(n, _UTF8) + 1
            buf = create_string_buffer(size)
            return buf.value.decode("utf-8", "replace") if self.CFStringGetCString(ref, buf, size, _UTF8) else None
        if tid == self.T_BOOL:
            return bool(self.CFBooleanGetValue(ref))
        if tid == self.T_NUM:
            d = c_double()
            self.CFNumberGetValue(ref, 13, byref(d))  # kCFNumberDoubleType
            return d.value
        if tid == self.T_AXVALUE:
            kind = self.AXValueGetType(ref)
            if kind == 1:
                p = CGPoint(); self.AXValueGetValue(ref, kind, byref(p)); return (p.x, p.y)
            if kind == 2:
                s = CGSize(); self.AXValueGetValue(ref, kind, byref(s)); return (s.w, s.h)
            return None
        return ("ref", ref)

    def attr(self, elem, name: str) -> tuple[Any, int]:
        """One attribute as a python value; a CF array/element answers ('ref', ptr) RETAINED for the caller."""
        out = c_void_p()
        err = self.AXUIElementCopyAttributeValue(elem, self.cfs(name), byref(out))
        if err != 0 or not out.value:
            return None, err
        val = self.to_py(out.value)
        if isinstance(val, tuple) and val and val[0] == "ref":
            return val, 0
        self.CFRelease(out.value)
        return val, 0

    def multi(self, elem, names: list[str]) -> tuple[int, list[Any]]:
        arr = self.CFArrayCreate(None, (c_void_p * len(names))(*[self.cfs(n) for n in names]), len(names), self.ARRAY_CB)
        out = c_void_p()
        try:
            err = self.AXUIElementCopyMultipleAttributeValues(elem, arr, 0, byref(out))
        finally:
            self.CFRelease(arr)
        if err != 0 or not out.value:
            return err, []
        vals = []
        for i in range(len(names)):
            v = self.to_py(self.CFArrayGetValueAtIndex(out.value, i))
            vals.append(None if isinstance(v, tuple) and v and v[0] == "ref" else v)
        self.CFRelease(out.value)
        return 0, vals

    def children(self, elem, limit: int) -> tuple[int, list[int]]:
        """Bounded child fetch (ranged read; plain read when an app rejects the range form) -> RETAINED refs."""
        out = c_void_p()
        err = self.AXUIElementCopyAttributeValues(elem, self.cfs("AXChildren"), 0, limit, byref(out))
        if err == -25201:
            err = self.AXUIElementCopyAttributeValue(elem, self.cfs("AXChildren"), byref(out))
        if err != 0 or not out.value:
            return err, []
        try:
            n = min(self.CFArrayGetCount(out.value), limit)
            return 0, [self.CFRetain(self.CFArrayGetValueAtIndex(out.value, i)) for i in range(n)]
        finally:
            self.CFRelease(out.value)

    def windows(self, app_elem) -> list[dict[str, Any]]:
        val, err = self.attr(app_elem, "AXWindows")
        if err != 0 or not val:
            return []
        arr = val[1]
        rows = []
        try:
            for i in range(self.CFArrayGetCount(arr)):
                win = self.CFArrayGetValueAtIndex(arr, i)
                _e, vals = self.multi(win, ["AXTitle", "AXMinimized", "AXPosition", "AXSize", "AXMain"])
                title, minimized, pos, size, main = (vals + [None] * 5)[:5]
                row: dict[str, Any] = {"title": title, "minimized": bool(minimized), "main": bool(main)}
                if isinstance(pos, tuple) and isinstance(size, tuple):
                    row["bounds"] = [int(pos[0]), int(pos[1]), int(size[0]), int(size[1])]
                if self.AXUIElementGetWindow is not None:
                    wid = c_uint32()
                    if self.AXUIElementGetWindow(win, byref(wid)) == 0:
                        row["window_id"] = int(wid.value)
                rows.append(row)
        finally:
            self.CFRelease(arr)
        return rows


def walk(root: Any, read_node: Callable[[Any], tuple[int, list[Any]]], children_of: Callable[[Any, int], tuple[int, list[Any]]],
         *, max_nodes: int, deadline_s: float, fanout: int = 80, skip_children: tuple[str, ...] = ("AXWebArea",),
         release: Callable[[Any], None] | None = None, now: Callable[[], float] = time.perf_counter) -> dict[str, Any]:
    """Bounded breadth-first walk over an element graph (injectable, so the bound is testable without a desktop).

    ``read_node(el)`` -> (err, [role, subrole, title, description, value, position, size]); ``children_of(el, n)`` ->
    (err, [children]). Stops at ``max_nodes`` visited nodes or at the deadline and SAYS which (``truncated_by``);
    children of ``skip_children`` roles (web content) are not fetched unless the caller clears the tuple.
    """
    t0 = now()
    deadline = t0 + deadline_s
    queue: deque[tuple[Any, int]] = deque([(root, 0)])
    nodes: list[dict[str, Any]] = []
    visited = 0
    errors: dict[str, int] = {}
    truncated_by: list[str] = []
    skipped_web = 0
    try:
        while queue:
            if visited >= max_nodes:
                truncated_by.append("max_nodes")
                break
            if now() > deadline:
                truncated_by.append("timeout")
                break
            el, depth = queue.popleft()
            err, vals = read_node(el)
            if err != 0:
                key = AX_ERR.get(err, str(err))
                errors[key] = errors.get(key, 0) + 1
                if release:
                    release(el)
                continue
            visited += 1
            role, subrole, title, desc, value, pos, size = (list(vals) + [None] * 7)[:7]
            node: dict[str, Any] = {"id": visited, "depth": depth, "role": (role or "")[2:] if isinstance(role, str) and role.startswith("AX") else role}
            if subrole:
                node["subrole"] = subrole
            label = title or desc or ""
            if label:
                node["label"] = str(label)[:200]
            if isinstance(value, (str, float, bool)) and value != "":
                text = str(value)
                node["value"] = text[:200]
                if len(text) > 200:
                    node["value_len"] = len(text)
            if isinstance(pos, tuple) and isinstance(size, tuple):
                node["x"], node["y"], node["w"], node["h"] = int(pos[0]), int(pos[1]), int(size[0]), int(size[1])
            nodes.append(node)
            if role in skip_children:
                skipped_web += 1
            else:
                cerr, kids = children_of(el, fanout)
                if cerr not in (0, -25212, -25205):
                    key = "children:" + AX_ERR.get(cerr, str(cerr))
                    errors[key] = errors.get(key, 0) + 1
                for kid in kids:
                    queue.append((kid, depth + 1))
            if release:
                release(el)
    finally:
        if release:
            for el, _depth in queue:
                release(el)
    if skipped_web:
        truncated_by.append("web_content_skipped")
    return {"nodes": nodes, "visited": visited, "truncated": bool(truncated_by), "truncated_by": truncated_by,
            "errors": errors, "elapsed_ms": int((now() - t0) * 1000)}


def marks_from(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set-of-marks view: numbered actionable elements with centre points in global logical points."""
    marks = []
    for node in nodes:
        if "AX" + str(node.get("role")) in ACTIONABLE and "x" in node and node.get("w", 0) > 0 and node.get("h", 0) > 0:
            marks.append({"id": len(marks) + 1, "role": node["role"], "title": node.get("label", ""),
                          "center_x": node["x"] + node["w"] // 2, "center_y": node["y"] + node["h"] // 2,
                          "x": node["x"], "y": node["y"], "w": node["w"], "h": node["h"]})
    return marks


# ----------------------------------------------------------------------------------------------
# operations
# ----------------------------------------------------------------------------------------------

def op_front(ax: _AX | None = None) -> dict[str, Any]:
    front = front_app()
    if not front:
        return {"ok": False, "error": "lsappinfo gave no frontmost application"}
    out: dict[str, Any] = {"ok": True, **front}
    ax = ax or _AX()
    if ax.AXIsProcessTrusted():
        app = ax.AXUIElementCreateApplication(int(front["pid"]))
        ax.AXUIElementSetMessagingTimeout(app, 0.3)
        win, err = ax.attr(app, "AXFocusedWindow")
        if err == 0 and win:
            title, _e = ax.attr(win[1], "AXTitle")
            ax.CFRelease(win[1])
            out["title"] = title
        else:
            out["title"] = None
            out["title_unread"] = AX_ERR.get(err, str(err))
        ax.CFRelease(app)
    else:
        out["title"] = None
        out["title_unread"] = "accessibility_untrusted"
    return out


def op_apps(limit: int = 50, ax: _AX | None = None, with_windows: bool = True) -> dict[str, Any]:
    apps = running_apps()
    front = front_app() or {}
    ax = ax or _AX()
    trusted = bool(ax.AXIsProcessTrusted())
    rows = []
    for app in apps[: max(1, limit)]:
        row: dict[str, Any] = {"app": app["app"], "pid": app["pid"], "app_id": app["app_id"],
                               "frontmost": app["pid"] == front.get("pid")}
        s = scriptable(app["path"])
        if s is not None:
            row["scriptable"] = s
        if with_windows and trusted:
            elem = ax.AXUIElementCreateApplication(int(app["pid"]))
            ax.AXUIElementSetMessagingTimeout(elem, 0.25)
            try:
                row["windows"] = ax.windows(elem)
            finally:
                ax.CFRelease(elem)
        rows.append(row)
    return {"ok": True, "frontmost": front or None, "apps": rows, "titles_available": trusted,
            "truncated": len(apps) > len(rows), "limit": limit, "total": len(apps)}


def op_resolve(query: str) -> dict[str, Any]:
    apps = running_apps()
    matches = resolve(query, apps)
    out = {"ok": True, "query": query, "candidates": [{"app": a["app"], "pid": a["pid"], "app_id": a["app_id"]} for a in matches]}
    if not matches:
        out["ok"] = False
        out["reason"] = "not_running"
        out["running"] = [a["app"] for a in apps][:40]
    elif len(matches) > 1:
        out["ok"] = False
        out["reason"] = "ambiguous"
    return out


def _activate_pid(ax: _AX, pid: int) -> str:
    """Ask the OS to bring ``pid`` forward; returns the mechanism tried. The caller re-reads who is in front."""
    if ax.AXIsProcessTrusted():
        app = ax.AXUIElementCreateApplication(pid)
        try:
            ax.AXUIElementSetMessagingTimeout(app, 0.5)
            if ax.AXUIElementSetAttributeValue(app, ax.cfs("AXFrontmost"), ax.kCFBooleanTrue) == 0:
                return "ax_frontmost"
        finally:
            ax.CFRelease(app)
    try:
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication  # type: ignore

        running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if running is not None and running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
            return "nsrunningapplication"
    except Exception:  # noqa: BLE001 - pyobjc absent or refused; the next mechanism follows
        pass
    _sh(["/usr/bin/osascript", "-e",
         f'tell application "System Events" to set frontmost of (first process whose unix id is {int(pid)}) to true'], timeout=5)
    return "system_events"


def op_activate(query: str, wait_ms: int = 2000) -> dict[str, Any]:
    resolved = op_resolve(query)
    if not resolved["ok"]:
        return resolved
    target = resolved["candidates"][0]
    before = front_app() or None
    out: dict[str, Any] = {"ok": True, "target": target, "front_before": before}
    if before and before.get("pid") == target["pid"]:
        out.update({"activated": False, "already_front": True, "front": before, "waited_ms": 0})
        return out
    ax = _AX()
    method = _activate_pid(ax, int(target["pid"]))
    t0 = time.monotonic()
    front = front_app()
    while (not front or front.get("pid") != target["pid"]) and (time.monotonic() - t0) * 1000 < max(200, int(wait_ms)):
        time.sleep(0.05)
        front = front_app()
    waited = int((time.monotonic() - t0) * 1000)
    verified = bool(front and front.get("pid") == target["pid"])
    out.update({"activated": verified, "method": method, "front": front or None, "waited_ms": waited})
    if not verified:
        out["ok"] = False
        out["reason"] = "not_frontmost_after_activate"
    return out


def op_tree(pid: int, max_nodes: int = 150, timeout_ms: int = 3000, root: str = "window", include_web: bool = False) -> dict[str, Any]:
    ax = _AX()
    if not ax.AXIsProcessTrusted():
        return {"ok": False, "reason": "accessibility_untrusted", "pid": pid}
    max_nodes = max(1, min(int(max_nodes), TREE_CEILINGS["max_nodes"]))
    timeout_ms = max(100, min(int(timeout_ms), TREE_CEILINGS["timeout_ms"]))
    app = ax.AXUIElementCreateApplication(int(pid))
    ax.AXUIElementSetMessagingTimeout(app, min(1.0, timeout_ms / 1000.0))
    root_ref = None
    root_from = None
    try:
        for name in (("AXFocusedUIElement",) if root == "focused" else ()) + ("AXFocusedWindow", "AXMainWindow"):
            val, err = ax.attr(app, name)
            if err == 0 and val and isinstance(val, tuple):
                root_ref, root_from = val[1], name
                break
        if root_ref is None:
            val, err = ax.attr(app, "AXWindows")
            if err == 0 and val and isinstance(val, tuple):
                arr = val[1]
                if ax.CFArrayGetCount(arr) > 0:
                    root_ref, root_from = ax.CFRetain(ax.CFArrayGetValueAtIndex(arr, 0)), "AXWindows[0]"
                ax.CFRelease(arr)
            if root_ref is None:
                return {"ok": False, "reason": "no_window" if err in (0, -25212) else AX_ERR.get(err, str(err)), "pid": pid}
        title, _e = ax.attr(root_ref, "AXTitle")
        result = walk(root_ref, lambda el: ax.multi(el, _WALK_ATTRS), ax.children, max_nodes=max_nodes,
                      deadline_s=timeout_ms / 1000.0, skip_children=() if include_web else ("AXWebArea",),
                      release=ax.CFRelease)
        root_ref = None  # released by the walk
    finally:
        if root_ref is not None:
            ax.CFRelease(root_ref)
        ax.CFRelease(app)
    result.update({"ok": True, "pid": pid, "root": root_from, "window": title if isinstance(title, str) else None,
                   "marks": marks_from(result["nodes"]),
                   "limits": {"max_nodes": max_nodes, "timeout_ms": timeout_ms, "ceilings": TREE_CEILINGS}})
    return result


def op_perms() -> dict[str, Any]:
    ax = _AX()
    cg = ctypes.CDLL(_CG_PATH)
    out: dict[str, Any] = {"ok": True, "accessibility": "trusted" if ax.AXIsProcessTrusted() else "untrusted"}
    for key, name in (("screen_recording", "CGPreflightScreenCaptureAccess"), ("post_events", "CGPreflightPostEventAccess"),
                      ("listen_events", "CGPreflightListenEventAccess")):
        try:
            fn = getattr(cg, name)
            fn.restype = c_ubyte
            out[key] = "granted" if fn() else "denied"
        except Exception:  # noqa: BLE001
            out[key] = "unknown"
    return out


def op_displays() -> dict[str, Any]:
    cg = ctypes.CDLL(_CG_PATH)
    cg.CGMainDisplayID.restype = c_uint32
    cg.CGDisplayBounds.restype = CGRect
    cg.CGDisplayBounds.argtypes = [c_uint32]
    cg.CGDisplayPixelsWide.restype = c_size_t
    cg.CGDisplayPixelsWide.argtypes = [c_uint32]
    cg.CGDisplayPixelsHigh.restype = c_size_t
    cg.CGDisplayPixelsHigh.argtypes = [c_uint32]
    cg.CGGetActiveDisplayList.argtypes = [c_uint32, POINTER(c_uint32), POINTER(c_uint32)]
    count = c_uint32(0)
    ids = (c_uint32 * 16)()
    if cg.CGGetActiveDisplayList(16, ids, byref(count)) != 0:
        return {"ok": False, "error": "CGGetActiveDisplayList failed"}
    main = cg.CGMainDisplayID()
    rows = []
    for i in range(count.value):
        did = ids[i]
        r = cg.CGDisplayBounds(did)
        rows.append({"id": int(did), "main": did == main, "x": int(r.origin.x), "y": int(r.origin.y),
                     "width": int(r.size.w), "height": int(r.size.h),
                     "pixels_wide": int(cg.CGDisplayPixelsWide(did)), "pixels_high": int(cg.CGDisplayPixelsHigh(did)),
                     "index": i + 1})
    return {"ok": True, "displays": rows}


def main(argv: list[str]) -> int:
    if not argv:
        print(json.dumps({"ok": False, "error": "usage: macos_native.py <front|apps|resolve|activate|tree|perms|displays> [args]"}))
        return 2
    op, args = argv[0], argv[1:]
    try:
        if op == "front":
            out = op_front()
        elif op == "apps":
            out = op_apps(int(args[0]) if args else 50)
        elif op == "resolve":
            out = op_resolve(args[0] if args else "")
        elif op == "activate":
            out = op_activate(args[0] if args else "", int(args[1]) if len(args) > 1 else 2000)
        elif op == "tree":
            out = op_tree(int(args[0]), int(args[1]) if len(args) > 1 else 150, int(args[2]) if len(args) > 2 else 3000,
                          args[3] if len(args) > 3 else "window", (args[4].lower() == "true") if len(args) > 4 else False)
        elif op == "perms":
            out = op_perms()
        elif op == "displays":
            out = op_displays()
        else:
            out = {"ok": False, "error": f"unknown op {op!r}"}
    except Exception as exc:  # noqa: BLE001 - the plugin renders the failure; nothing here may crash silently
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "op": op}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
