"""unix_computer_use 0.5.3: inputs addressed to an application, place receipts, bounded element reads.

The 27.10 incident class: the model pressed cmd+tab and an arrow key into whatever was in front and read
``observed_effect: true`` (the helper exited 0) as "the slide changed". Every test here fakes only the OS
boundary (``subprocess.run``: cliclick, lsappinfo, wmctrl/xdotool, the native helper's JSON) and asserts
both directions: the guard fires (nothing is sent, no mark) and stays quiet (one input, the mark, the place).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "unix_computer_use"


def load_plugin():
    spec = importlib.util.spec_from_file_location("unix_computer_use_targeting_plugin", SKILL / "plugin.py",
                                                  submodule_search_locations=[str(SKILL)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class API:
    def __init__(self, state: Path) -> None:
        self.state, self.tools, self.meta = state, {}, {}

    def get_state_dir(self) -> str:
        return str(self.state)

    def skill_job_dir(self, job_id: str) -> Path:
        path = self.state / "jobs" / job_id
        (path / "output").mkdir(parents=True, exist_ok=True)
        return path

    def register_tool(self, name, handler, **metadata):
        self.tools[name] = handler
        self.meta[name] = metadata


SAFARI = ("Safari", 100, "com.apple.Safari")
KEYNOTE = ("Keynote", 200, "com.apple.iWork.Keynote")


def helper_op(argv: list[str]) -> list[str]:
    """The native helper's op and args from its argv (``python -I macos_native.py <op> ...``)."""
    idx = next(i for i, part in enumerate(argv) if str(part).endswith("macos_native.py"))
    return [str(a) for a in argv[idx + 1:]]


def mac(tmp_path, monkeypatch, *, front=SAFARI, helper=None, cliclick=(0, "", ""), lsappinfo=True):
    """A faked local Mac: lsappinfo reports ``state['front']``; the native helper answers through ``helper(args, state)``."""
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "macos")
    monkeypatch.setattr(module, "_session_type", lambda: "native")
    tools = {"cliclick", "screencapture", "osascript", "pbcopy"} | ({"lsappinfo"} if lsappinfo else set())
    monkeypatch.setattr(module, "_which", lambda name: f"/usr/bin/{name}" if name in tools else "")
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    state = {"front": front, "calls": [], "helper": [], "clipboard": None}

    def default_helper(args, state):
        op = args[0]
        if op == "activate":
            query = args[1]
            if query in ("Keynote", "pid:200", "com.apple.iWork.Keynote"):
                before = dict(zip(("app", "pid", "app_id"), state["front"]))
                state["front"] = KEYNOTE
                target = dict(zip(("app", "pid", "app_id"), KEYNOTE))
                if before["pid"] == 200:
                    return {"ok": True, "target": target, "front_before": before, "front": target, "activated": False, "already_front": True, "waited_ms": 0}
                return {"ok": True, "target": target, "front_before": before, "front": target, "activated": True, "method": "ax_frontmost", "waited_ms": 120}
            if query == "Stuck":
                before = dict(zip(("app", "pid", "app_id"), state["front"]))
                return {"ok": False, "reason": "not_frontmost_after_activate", "target": {"app": "Stuck", "pid": 300, "app_id": "x.stuck"},
                        "front_before": before, "front": before, "activated": False, "method": "ax_frontmost", "waited_ms": 2000}
            if query == "Term":
                return {"ok": False, "reason": "ambiguous", "candidates": [{"app": "Terminal", "pid": 1}, {"app": "Termius", "pid": 2}]}
            return {"ok": False, "reason": "not_running", "running": ["Safari", "Keynote"], "candidates": []}
        if op == "resolve":
            if args[1] in ("Keynote", "pid:200"):
                return {"ok": True, "candidates": [dict(zip(("app", "pid", "app_id"), KEYNOTE))]}
            return {"ok": False, "reason": "not_running", "candidates": [], "running": ["Safari"]}
        if op == "perms":
            return {"ok": True, "accessibility": "trusted", "screen_recording": "granted", "post_events": "granted", "listen_events": "denied"}
        if op == "displays":
            return {"ok": True, "displays": [{"id": 1, "main": True, "x": 0, "y": 0, "width": 1728, "height": 1117, "index": 1},
                                             {"id": 2, "main": False, "x": 1728, "y": 0, "width": 1920, "height": 1080, "index": 2}]}
        if op == "tree":
            return {"ok": True, "pid": int(args[1]), "root": "AXFocusedWindow", "window": "Deck.key", "visited": 1, "truncated": False,
                    "truncated_by": [], "errors": {}, "elapsed_ms": 5, "marks": [],
                    "nodes": [{"id": 1, "depth": 0, "role": "Window", "label": "Deck.key", "x": 1800, "y": 100, "w": 800, "h": 600}],
                    "limits": {"max_nodes": int(args[2]), "timeout_ms": int(args[3])}}
        if op == "apps":
            return {"ok": True, "frontmost": dict(zip(("app", "pid", "app_id"), state["front"])), "titles_available": True,
                    "apps": [{"app": "Safari", "pid": 100, "app_id": "com.apple.Safari", "frontmost": state["front"][1] == 100, "scriptable": True,
                              "windows": [{"title": "Ouroboros", "minimized": False, "main": True, "bounds": [0, 0, 800, 600], "window_id": 39}]},
                             {"app": "Keynote", "pid": 200, "app_id": "com.apple.iWork.Keynote", "frontmost": state["front"][1] == 200,
                              "scriptable": True, "windows": [{"title": "Deck.key", "minimized": False, "main": True}]}],
                    "truncated": False, "limit": int(args[1]), "total": 2}
        return {"ok": False, "error": f"no fake for {op}"}

    answer = helper or default_helper

    def run(cmd, *a, **k):
        argv = [str(part) for part in cmd]
        state["calls"].append(argv)
        if argv[:2] == ["lsappinfo", "front"]:
            return subprocess.CompletedProcess(argv, 0, "ASN:0x0-0x1:\n", "")
        if argv[:2] == ["lsappinfo", "info"]:
            name, pid, bid = state["front"]
            return subprocess.CompletedProcess(argv, 0, f'"pid"={pid}\n"CFBundleIdentifier"="{bid}"\n"LSDisplayName"="{name}"\n', "")
        if any(part.endswith("macos_native.py") for part in argv):
            args = helper_op(argv)
            state["helper"].append(args)
            out = answer(args, state)
            return subprocess.CompletedProcess(argv, 0 if out.get("ok") else 1, json.dumps(out) + "\n", "")
        if argv[0] == "cliclick":
            rc, out, err = cliclick
            return subprocess.CompletedProcess(argv, rc, out, err)
        if argv[0] == "pbcopy":
            state["clipboard"] = k.get("input")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "screencapture":
            from PIL import Image
            Image.new("RGB", (64, 40), (1, 2, 3)).save(Path(argv[-1]), format="PNG")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    api = API(tmp_path / "state")
    module.register(api)
    return module, api, state


def inputs(state) -> list[list[str]]:
    return [argv for argv in state["calls"] if argv[0] == "cliclick"]


def receipt(text: str) -> dict:
    out = json.loads(text)
    assert isinstance(out, dict)
    return out


# --- an input aimed at an application (macOS) -----------------------------------------------------

def test_a_targeted_key_is_sent_only_after_the_target_is_verified_in_front(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    assert inputs(state) == [["cliclick", "kp:arrow-right"]], "exactly one input, after the switch"
    assert state["helper"][0][:2] == ["activate", "Keynote"], "one helper call resolves, activates and verifies"
    assert out["target"]["requested"] == "Keynote" and out["target"]["activated"] is True
    assert out["delivered_to"] == {"app": "Keynote", "pid": 200, "app_id": "com.apple.iWork.Keynote"}
    assert out["front_before"] == {"app": "Safari", "pid": 100, "app_id": "com.apple.Safari"}
    assert "front_after" not in out, "the front did not change again after the input"
    assert "no UI change" in out["note"] and "Keynote" in out["note"]


def test_a_target_that_did_not_come_forward_sends_nothing(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="right", app="Stuck"))
    assert out["ok"] is False and "observed_effect" not in out, out
    assert out["reason"] == "not_frontmost_after_activate" and "Safari" in out["error"] and "nothing was sent" in out["error"]
    assert inputs(state) == [], "the guard fires: no cliclick at all"


@pytest.mark.parametrize("query, reason", [("NoSuchApp", "not_running"), ("Term", "ambiguous")])
def test_an_unresolved_target_refuses_before_any_input(tmp_path, monkeypatch, query, reason):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["click"](x=10, y=20, raw=True, app=query))
    assert out["ok"] is False and out["reason"] == reason and "observed_effect" not in out, out
    assert inputs(state) == []
    if reason == "ambiguous":
        assert [c["app"] for c in out["candidates"]] == ["Terminal", "Termius"] and "pid:" in out["error"]


def test_an_untargeted_input_keeps_working_and_names_who_received_it(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="right"))
    assert out["ok"] is True and out["observed_effect"] is True
    assert inputs(state) == [["cliclick", "kp:arrow-right"]] and state["helper"] == [], "no activation without app="
    assert out["delivered_to"]["app"] == "Safari" and "target" not in out
    # app="" is what a voice model sends for an unused optional field: identical to omitting it.
    state["calls"].clear()
    out2 = receipt(api.tools["key"](keys="right", app=""))
    assert out2["delivered_to"]["app"] == "Safari" and state["helper"] == []


def test_without_lsappinfo_the_place_is_unknown_and_the_input_still_goes(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch, lsappinfo=False)
    out = receipt(api.tools["key"](keys="return"))
    assert out["ok"] is True and out["observed_effect"] is True
    assert out["delivered_to"] == "unknown" and "unknown" in out["note"]
    assert inputs(state) == [["cliclick", "kp:return"]]


def test_a_front_change_after_the_input_is_reported(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)

    def run_and_switch(cmd, *a, **k):
        argv = [str(part) for part in cmd]
        if argv[0] == "cliclick":
            state["front"] = KEYNOTE  # cmd+tab moved the front
        return original(cmd, *a, **k)
    original = module.subprocess.run
    monkeypatch.setattr(module.subprocess, "run", run_and_switch)
    out = receipt(api.tools["key"](keys="cmd+tab"))
    assert out["delivered_to"]["app"] == "Safari" and out["front_after"]["app"] == "Keynote", out


def test_a_dropped_input_is_an_honest_failure(tmp_path, monkeypatch):
    """cliclick exits 0 and only warns on stderr when macOS has no Accessibility grant for the posting process."""
    module, api, state = mac(tmp_path, monkeypatch,
                             cliclick=(0, "", "WARNING: Accessibility privileges not enabled. Many actions may fail.\n"))
    out = receipt(api.tools["key"](keys="right"))
    assert out["ok"] is False and "observed_effect" not in out, out
    assert out["reason"] == "permission_denied" and out["permission"] == {"accessibility": "untrusted", "source": "helper stderr"}
    assert "Accessibility" in out["helper_stderr"]
    # Other stderr noise is kept beside a successful dispatch.
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(0, "", "some note\n"))
    out = receipt(api.tools["key"](keys="right"))
    assert out["ok"] is True and out["observed_effect"] is True and out["helper_stderr"] == "some note"


def test_characters_the_layout_cannot_type_are_an_honest_failure(tmp_path, monkeypatch):
    """Measured on the live probe (27.10.2026): under a Cyrillic layout cliclick typed none of 'ouroboros probe',
    printed one 'Unable to get key code for <c>' line per letter and exited 0."""
    stderr = "".join(f"Unable to get key code for {c} (see README-Characters.md)\n" for c in "ourbs")
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(0, "", stderr))
    out = receipt(api.tools["type_text"](text="ouroboros probe"))
    assert out["ok"] is False and "observed_effect" not in out, out
    assert out["reason"] == "untypable_characters" and out["untyped"] == list("ourbs") and "via=paste" in out["error"]
    module, api, state = mac(tmp_path, monkeypatch)
    assert receipt(api.tools["type_text"](text="ouroboros probe"))["observed_effect"] is True, "a clean dispatch keeps the mark"


def test_an_activation_that_ran_before_a_failed_input_keeps_its_mark(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(1, "", "boom"))
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["observed_effect"] is True and out["activation_done"] is True, out
    assert out["target"]["activated"] is True and out["error"] == "boom"


def test_a_key_sequence_brings_the_target_forward_once(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="down down return", app="Keynote"))
    assert out["ok"] is True and out["steps"] == 3 and out["observed_effect"] is True
    assert [args[0] for args in state["helper"]] == ["activate"], "one activation for the whole sequence"
    assert len(inputs(state)) == 3 and out["target"]["activated"] is True and out["delivered_to"]["app"] == "Keynote"


def test_mouse_up_and_move_never_change_the_front(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    for tool in ("mouse_up", "move"):
        assert "app" not in api.meta[tool]["schema"]["properties"], tool
    out = receipt(api.tools["mouse_down"](x=5, y=6, raw=True, app="Keynote"))
    assert out["ok"] is True and out["target"]["activated"] is True and inputs(state)[-1] == ["cliclick", "dd:5,6"]


# --- activate_app ---------------------------------------------------------------------------------

def test_activate_app_marks_only_a_switch_it_observed(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["activate_app"](app="Keynote"))
    assert out["ok"] is True and out["activated"] is True and out["observed_effect"] is True
    assert out["front"]["app"] == "Keynote" and out["front_before"]["app"] == "Safari" and inputs(state) == []
    again = receipt(api.tools["activate_app"](app="Keynote"))
    assert again["ok"] is True and again["activated"] is False and "observed_effect" not in again, "already in front: no effect"
    refused = receipt(api.tools["activate_app"](app="Stuck"))
    assert refused["ok"] is False and "observed_effect" not in refused and refused["reason"] == "not_frontmost_after_activate"
    assert receipt(api.tools["activate_app"](app=""))["ok"] is False


# --- window_list, capabilities, ax_tree ------------------------------------------------------------

def test_window_list_names_the_front_and_the_scriptable_apps(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["window_list"]())
    assert out["ok"] is True and out["frontmost"]["app"] == "Safari" and out["titles_available"] is True
    rows = {row["app"]: row for row in out["windows"]}
    assert rows["Keynote"]["frontmost"] is False and rows["Keynote"]["scriptable"] is True
    assert rows["Safari"]["windows"][0]["title"] == "Ouroboros"
    only = receipt(api.tools["window_list"](app="keynote"))
    assert [row["app"] for row in only["windows"]] == ["Keynote"]
    # The helper not answering is a failure, not an empty list.
    module, api, state = mac(tmp_path, monkeypatch, helper=lambda args, st: {"ok": False, "error": "no answer"})
    out = receipt(api.tools["window_list"]())
    assert out["ok"] is False and "windows" not in out and out["frontmost"]["app"] == "Safari"


def test_capabilities_report_real_grants_and_targeting(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    caps = receipt(api.tools["capabilities"]())
    assert caps["permission_state_verified"] is True
    assert caps["permissions"] == {"accessibility": "trusted", "screen_recording": "granted", "post_events": "granted", "listen_events": "denied"}
    assert caps["frontmost"]["app"] == "Safari" and caps["targeting"] == {"app": "supported"}
    module, api, state = mac(tmp_path, monkeypatch, helper=lambda args, st: {"ok": False, "error": "no answer"})
    caps = receipt(api.tools["capabilities"]())
    assert caps["permission_state_verified"] is False and "error" in caps["permissions"]


def test_ax_tree_reads_a_named_app_without_bringing_it_forward(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["ax_tree"](app="Keynote", max_nodes=40, timeout_ms=500))
    assert out["ok"] is True and out["app"]["pid"] == 200 and out["window"] == "Deck.key"
    assert [args[0] for args in state["helper"]] == ["resolve", "tree"], "resolve, then read; never activate"
    assert state["helper"][1][1:] == ["200", "40", "500", "window", "false"]
    assert state["front"] == SAFARI, "reading does not move the front"
    missing = receipt(api.tools["ax_tree"](app="Nope"))
    assert missing["ok"] is False and missing["reason"] == "not_running"


# --- type_text via=paste and the display transform -------------------------------------------------

def test_type_text_via_paste_writes_the_clipboard_then_pastes(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["type_text"](text="Привет, мир " * 40, via="paste", app="Keynote"))
    assert out["ok"] is True and out["method"] == "paste" and out["clipboard_replaced"] is True
    assert state["clipboard"].startswith("Привет") and inputs(state) == [["cliclick", "kd:cmd", "t:v", "ku:cmd"]]
    assert out["delivered_to"]["app"] == "Keynote"
    plain = receipt(api.tools["type_text"](text="hi"))
    assert plain["ok"] is True and inputs(state)[-1] == ["cliclick", "t:hi"] and "method" not in plain


def test_screenshot_transform_uses_display_bounds_and_the_target_display(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_png_dimensions", lambda _p: (3456, 2234))
    out = receipt(api.tools["screenshot"](job_id="main"))
    assert out["ok"] is True and out["display"]["index"] == 1
    tf = out["coord_transform"]
    assert (tf["input_w"], tf["input_h"], tf["ox"], tf["oy"]) == (1728, 1117, 0, 0) and tf["approx"] is False
    assert inputs(state) == [] and ["screencapture", "-x", out["full_resolution_path"]] in state["calls"]
    # app= : the display holding that app's window is captured and the transform carries its origin.
    out = receipt(api.tools["screenshot"](job_id="deck", app="Keynote"))
    assert out["display"]["index"] == 2 and out["app"]["pid"] == 200
    assert any(argv[:4] == ["screencapture", "-x", "-D", "2"] for argv in state["calls"])
    assert (out["coord_transform"]["ox"], out["coord_transform"]["oy"]) == (1728, 0)
    click = receipt(api.tools["click"](x=100, y=50))
    assert click["ok"] is True
    sx = out["coord_transform"]["sx"]
    assert inputs(state)[-1] == ["cliclick", f"c:{round(100 * sx) + 1728},{round(50 * out['coord_transform']['sy'])}"]


def test_screenshot_falls_back_to_the_old_transform_when_displays_are_unreadable(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch, helper=lambda args, st: {"ok": False, "error": "locked"})
    monkeypatch.setattr(module, "_png_dimensions", lambda _p: (800, 600))
    monkeypatch.setattr(module, "_macos_logical_size", lambda: (0, 0))
    out = receipt(api.tools["screenshot"](job_id="fallback"))
    assert out["ok"] is True and "display" not in out and out["coord_transform"]["approx"] is True


# --- registration -------------------------------------------------------------------------------

def test_the_surface_is_24_tools_with_app_on_the_inputs(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    assert "activate_app" in api.tools and "clear_active_connection" not in api.tools
    assert len(api.tools) == 24
    for tool in ("click", "double_click", "triple_click", "left_click_drag", "mouse_down", "type_text", "key", "hold_key", "scroll"):
        assert api.meta[tool]["schema"]["properties"]["app"] == {"type": "string", "default": ""}, tool
    assert api.meta["key"]["description"].startswith("Press keys in the frontmost app, or in app=")
    assert "run_command" in api.meta["key"]["description"]


# --- Linux X11 ----------------------------------------------------------------------------------

WMCTRL = ("0x001  0 4242 10 20 800 600 navigator.Firefox host Browser - Docs\n"
          "0x002  0 5151 0 0 1024 768 keynote.Keynote host Deck\n"
          "0x003  0 5151 0 0 300 200 keynote.Keynote host Inspector\n")


def x11(tmp_path, monkeypatch, *, active=("4242", "navigator.Firefox", "Browser - Docs"), xdotool=True, switch_to=None):
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_session_type", lambda: "x11")
    tools = {"wmctrl"} | ({"xdotool"} if xdotool else set())
    monkeypatch.setattr(module, "_which", lambda name: f"/usr/bin/{name}" if name in tools else "")
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    state = {"active": active, "calls": []}

    def run(cmd, *a, **k):
        argv = [str(part) for part in cmd]
        state["calls"].append(argv)
        if argv == ["wmctrl", "-lpxG"]:
            return subprocess.CompletedProcess(argv, 0, WMCTRL, "")
        if argv[:3] == ["wmctrl", "-i", "-a"]:
            if switch_to is not None:
                state["active"] = switch_to
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["xdotool", "getactivewindow"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(state["active"]) + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    api = API(tmp_path / "state")
    module.register(api)
    return module, api, state


def test_x11_targeted_key_activates_through_wmctrl_and_verifies_with_xdotool(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch, switch_to=("5151", "keynote.Keynote", "Deck"))
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    assert ["wmctrl", "-i", "-a", "0x002"] in state["calls"]
    assert [argv for argv in state["calls"] if argv[:2] == ["xdotool", "key"]] == [["xdotool", "key", "Right"]]
    assert out["target"]["activated"] is True and out["delivered_to"]["pid"] == 5151 and out["front_before"]["pid"] == 4242
    module, api, state = x11(tmp_path, monkeypatch)  # Firefox is already in front: no switch, one key
    already = receipt(api.tools["key"](keys="right", app="Firefox"))
    assert already["ok"] is True and already["target"]["activated"] is False
    assert not any(argv[:3] == ["wmctrl", "-i", "-a"] for argv in state["calls"])


def test_x11_refuses_when_the_switch_cannot_be_verified_or_matched(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch)  # wmctrl runs, xdotool keeps answering Firefox
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "not_frontmost_after_activate" and "observed_effect" not in out
    assert not any(argv[:2] == ["xdotool", "key"] for argv in state["calls"])
    missing = receipt(api.tools["key"](keys="right", app="Emacs"))
    assert missing["ok"] is False and missing["reason"] == "not_running" and "keynote.Keynote" in missing["running"]


def test_x11_without_xdotool_sends_after_an_unverifiable_switch(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch, xdotool=False)
    out = receipt(api.tools["type_text"](text="hi", app="Keynote"))
    assert out["ok"] is False and "backend" in out["error"], "typing on X11 needs xdotool: an honest backend error"


def test_x11_window_list_rows_and_wayland_refusal(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch)
    out = receipt(api.tools["window_list"]())
    assert out["ok"] is True and out["frontmost"]["pid"] == 4242
    assert [(r["app"], r["frontmost"]) for r in out["windows"]] == [("navigator.Firefox", True), ("keynote.Keynote", False), ("keynote.Keynote", False)]
    monkeypatch.setattr(module, "_session_type", lambda: "wayland")
    monkeypatch.setattr(module, "_which", lambda name: "")
    refusal = receipt(api.tools["window_list"]())
    assert refusal["ok"] is False and refusal["reason"] == "unsupported" and "Wayland" in refusal["error"]
    assert receipt(api.tools["key"](keys="right", app="Keynote"))["ok"] is False


# --- the native helper's pure parts (no desktop needed) ----------------------------------------

def test_native_parsers_and_resolution():
    from skills.unix_computer_use import macos_native as native

    info = native.parse_info('"pid"=406\n"CFBundleIdentifier"="com.apple.loginwindow"\n"LSDisplayName"="loginwindow"\n')
    assert info == {"pid": 406, "app_id": "com.apple.loginwindow", "app": "loginwindow"}
    listing = (' 1) "loginwindow" ASN:0x0-0x3003: \n    bundleID="com.apple.loginwindow"\n    bundle path="/System/Library/CoreServices/loginwindow.app"\n'
               '    pid = 406 type="UIElement" flavor=3\n'
               ' 2) "Терминал" ASN:0x0-0x5005: \n    bundleID="com.apple.Terminal"\n    bundle path="/System/Applications/Utilities/Terminal.app"\n'
               '    pid = 878 type="Foreground" flavor=3\n'
               ' 3) "Keynote" ASN:0x0-0x6006: \n    bundleID="com.apple.iWork.Keynote"\n    bundle path="/Applications/Keynote.app"\n'
               '    pid = 900 type="Foreground" flavor=3\n')
    apps = native.parse_list(listing)
    assert [a["app"] for a in apps] == ["Терминал", "Keynote"], "background-only processes are not applications"
    assert native.resolve("keynote", apps)[0]["pid"] == 900
    assert native.resolve("COM.APPLE.TERMINAL", apps)[0]["pid"] == 878
    assert native.resolve("Terminal", apps)[0]["pid"] == 878, "the executable's bundle name matches a localized display name"
    assert native.resolve("pid:900", apps)[0]["app"] == "Keynote"
    assert native.resolve("pid:1", apps) == [] and native.resolve("", apps) == []
    assert native.resolve("Key", apps) == [], "no prefix guessing: an unknown name resolves to nothing"


def test_native_walk_is_bounded_and_says_which_bound_cut_it():
    from skills.unix_computer_use import macos_native as native

    graph = {"root": ["a", "b", "web"], "a": [f"a{i}" for i in range(50)], "b": [], "web": ["w1", "w2"]}

    def read(el):
        role = "AXWebArea" if el == "web" else ("AXWindow" if el == "root" else "AXButton")
        return 0, [role, None, el, None, None, (10, 20), (30, 40)]

    def children(el, n):
        return 0, list(graph.get(el, []))[:n]

    clock = iter([0.0] * 1000)
    small = native.walk("root", read, children, max_nodes=5, deadline_s=10, now=lambda: next(clock))
    assert small["visited"] == 5 and small["truncated"] is True and small["truncated_by"] == ["max_nodes", "web_content_skipped"]
    whole = native.walk("root", read, children, max_nodes=100, deadline_s=10, now=lambda: next(clock))
    assert whole["visited"] == 54 and whole["truncated_by"] == ["web_content_skipped"], "web content is skipped and named"
    with_web = native.walk("root", read, children, max_nodes=100, deadline_s=10, skip_children=(), now=lambda: next(clock))
    assert with_web["visited"] == 56 and with_web["truncated"] is False
    ticks = iter([0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 5.0])
    late = native.walk("root", read, children, max_nodes=100, deadline_s=1, now=lambda: next(ticks, 5.0))
    assert late["truncated_by"][0] == "timeout" and late["visited"] >= 1
    marks = native.marks_from(whole["nodes"])
    assert marks and marks[0] == {"id": 1, "role": "Button", "title": "a", "center_x": 25, "center_y": 40, "x": 10, "y": 20, "w": 30, "h": 40}
    assert all(m["role"] != "Window" for m in marks) and len(marks) == 52

    def busy(el):
        return (-25204, []) if el == "a" else read(el)
    partial = native.walk("root", busy, children, max_nodes=100, deadline_s=10, now=lambda: next(clock))
    assert partial["errors"] == {"CannotComplete": 1} and partial["visited"] == 3, "a busy node is counted, not walked"


# --- remote targets: OSWorld guest and a Mac over SSH ------------------------------------------

class Guest:
    """``urlopen`` toward an OSWorld guest; the bash activation and the python action each answer once."""

    def __init__(self, answers):
        self.answers, self.commands = list(answers), []

    def __call__(self, request, timeout=None):
        command = json.loads(request.data.decode("utf-8"))["command"]
        self.commands.append(command)
        returncode, output = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        body = {"status": "success", "output": output, "error": "", "returncode": returncode}

        class Response:
            status = 200

            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Response(json.dumps(body).encode("utf-8"))


def osworld(tmp_path, monkeypatch, answers):
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_session_type", lambda: "x11")
    monkeypatch.setattr(module, "_which", lambda name: "")
    api = API(tmp_path / "state")
    module.register(api)
    api.tools["add_connection"](name="vm", backend="osworld_http", target="http://guest.invalid:5000", activate=True)
    guest = Guest(answers)
    monkeypatch.setattr(urllib.request, "urlopen", guest)
    return module, api, guest


def test_osworld_targeted_input_activates_in_the_guest_and_reads_the_front(tmp_path, monkeypatch):
    module, api, guest = osworld(tmp_path, monkeypatch, [
        (0, "__FRONT_BEFORE__4242|Browser\n__FRONT_AFTER__5151|Deck - Keynote\n"),
        (0, "__FRONT_BEFORE__5151|Deck - Keynote\n__FRONT_AFTER__5151|Deck - Keynote\n"),
    ])
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    assert guest.commands[0][:2] == ["bash", "-lc"] and "wmctrl -a Keynote" in guest.commands[0][2]
    assert "pyautogui.press('right')" in guest.commands[1][2] and "__FRONT_BEFORE__" in guest.commands[1][2]
    assert out["target"]["activated"] is True and out["delivered_to"] == {"pid": 5151, "title": "Deck - Keynote"}
    assert out["front_before"] == {"pid": 4242, "title": "Browser"}


def test_osworld_refuses_when_the_guest_window_did_not_come_forward(tmp_path, monkeypatch):
    module, api, guest = osworld(tmp_path, monkeypatch, [(0, "__FRONT_BEFORE__4242|Browser\n__FRONT_AFTER__4242|Browser\n")])
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "not_frontmost_after_activate" and "observed_effect" not in out
    assert len(guest.commands) == 1, "the action was never sent"
    module, api, guest = osworld(tmp_path, monkeypatch, [(1, "__FRONT_BEFORE__4242|Browser\n__FRONT_AFTER__4242|Browser\n")])
    out = receipt(api.tools["click"](x=1, y=2, raw=True, app="Keynote"))
    assert out["ok"] is False and out["reason"] == "not_running" and len(guest.commands) == 1


def test_osworld_untargeted_input_still_names_the_front_window(tmp_path, monkeypatch):
    module, api, guest = osworld(tmp_path, monkeypatch, [(0, "__FRONT_BEFORE__4242|Browser\n__FRONT_AFTER__4242|Browser\n")])
    out = receipt(api.tools["key"](keys="return"))
    assert out["ok"] is True and out["delivered_to"] == {"pid": 4242, "title": "Browser"} and "front_after" not in out
    assert len(guest.commands) == 1 and "pyautogui.press('enter')" in guest.commands[0][2]
    rows = receipt(api.tools["window_list"]())
    assert rows["ok"] is True  # the same guest answer is parsed leniently: no rows, no invented front
    assert rows["windows"] == [] and rows["frontmost"] is None


def ssh(tmp_path, monkeypatch, script):
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_session_type", lambda: "x11")
    monkeypatch.setattr(module, "_which", lambda name: "")
    calls = []

    def run(cmd, *a, **k):
        argv = [str(part) for part in cmd]
        calls.append(argv)
        rc, out = script(argv)
        return subprocess.CompletedProcess(argv, rc, out, "")
    monkeypatch.setattr(module.subprocess, "run", run)
    api = API(tmp_path / "state")
    module.register(api)
    api.tools["add_connection"](name="mac", backend="ssh_macos", host="mac.invalid", user="owner", activate=True)
    return module, api, calls


def test_ssh_targeted_input_activates_by_name_and_reads_the_front(tmp_path, monkeypatch):
    def script(argv):
        command = argv[-1]
        if "to activate" in command:
            return 0, '__FRONT_BEFORE__"pid"=100 "CFBundleIdentifier"="com.apple.Safari" "LSDisplayName"="Safari" \n__FRONT_AFTER__"pid"=200 "CFBundleIdentifier"="com.apple.iWork.Keynote" "LSDisplayName"="Keynote" \n'
        if "cliclick" in command:
            return 0, '__FRONT_BEFORE__"pid"=200 "CFBundleIdentifier"="com.apple.iWork.Keynote" "LSDisplayName"="Keynote" \n__FRONT_AFTER__"pid"=200 "CFBundleIdentifier"="com.apple.iWork.Keynote" "LSDisplayName"="Keynote" \n'
        return 0, ""
    module, api, calls = ssh(tmp_path, monkeypatch, script)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    assert 'tell application "Keynote" to activate' in calls[0][-1] and "cliclick kp:arrow-right" in calls[1][-1]
    assert out["delivered_to"]["app"] == "Keynote" and out["target"]["activated"] is True and out["front_before"]["app"] == "Safari"
    assert out["output"] == "", "the probe markers never reach the model as command output"
    bad = receipt(api.tools["key"](keys="right", app='Key"note'))
    assert bad["ok"] is False and bad["reason"] == "unsupported", "only a plain application name is interpolated"


def test_ssh_refuses_when_the_mac_kept_another_app_in_front(tmp_path, monkeypatch):
    def script(argv):
        return 0, '__FRONT_BEFORE__"pid"=100 "CFBundleIdentifier"="com.apple.Safari" "LSDisplayName"="Safari" \n__FRONT_AFTER__"pid"=100 "CFBundleIdentifier"="com.apple.Safari" "LSDisplayName"="Safari" \n'
    module, api, calls = ssh(tmp_path, monkeypatch, script)
    out = receipt(api.tools["type_text"](text="x", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "not_frontmost_after_activate" and "observed_effect" not in out
    assert len(calls) == 1 and "cliclick" not in calls[0][-1]
