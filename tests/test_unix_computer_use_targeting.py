"""unix_computer_use 0.5.3: inputs addressed to an application, place receipts, bounded element reads.

The 27.10 incident class: the model pressed cmd+tab and an arrow key into whatever was in front and read
``observed_effect: true`` (the helper exited 0) as "the slide changed". Every test here fakes only the OS
boundary (``subprocess.run``: cliclick, lsappinfo, wmctrl/xdotool, the native helper's JSON; ``urlopen`` toward
an OSWorld guest; ``ssh``) and asserts both directions: the guard fires (nothing is sent, no mark) and stays
quiet (one input, the mark, the place). The same receipt contract is pinned on every transport.
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


def app_dict(triple) -> dict:
    return dict(zip(("app", "pid", "app_id"), triple))


def helper_op(argv: list[str]) -> list[str]:
    """The native helper's op and args from its argv (``python -I macos_native.py <op> ...``)."""
    idx = next(i for i, part in enumerate(argv) if str(part).endswith("macos_native.py"))
    return [str(a) for a in argv[idx + 1:]]


def mac(tmp_path, monkeypatch, *, front=SAFARI, helper=None, cliclick=(0, "", ""), lsappinfo=True, on_input=None):
    """A faked local Mac: lsappinfo reports ``state['front']``; the native helper answers through ``helper(args, state)``;
    ``on_input(argv, state)`` runs when cliclick is called (a key that moves the front, for instance)."""
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
                before = app_dict(state["front"])
                state["front"] = KEYNOTE
                target = app_dict(KEYNOTE)
                if before["pid"] == 200:
                    return {"ok": True, "target": target, "front_before": before, "front": target, "activated": False,
                            "already_front": True, "verified": True, "waited_ms": 0}
                return {"ok": True, "target": target, "front_before": before, "front": target, "activated": True,
                        "verified": True, "method": "ax_frontmost", "waited_ms": 120}
            if query == "Stuck":
                before = app_dict(state["front"])
                return {"ok": False, "reason": "not_frontmost_after_activate", "target": {"app": "Stuck", "pid": 300, "app_id": "x.stuck"},
                        "front_before": before, "front": before, "activated": False, "verified": False, "method": "system_events", "waited_ms": 2000}
            if query == "Term":
                return {"ok": False, "reason": "ambiguous", "candidates": [{"app": "Terminal", "pid": 1}, {"app": "Termius", "pid": 2}]}
            return {"ok": False, "reason": "not_running", "running": ["Safari", "Keynote"], "candidates": []}
        if op == "resolve":
            if args[1] in ("Keynote", "pid:200"):
                return {"ok": True, "candidates": [app_dict(KEYNOTE)]}
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
            return {"ok": True, "frontmost": app_dict(state["front"]), "titles_available": True,
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
            if on_input:
                on_input(argv, state)
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
    assert out["target"]["requested"] == "Keynote" and out["target"]["activated"] is True and out["target"]["verified"] is True
    assert out["delivered_to"] == app_dict(KEYNOTE)
    assert out["front_before"] == app_dict(SAFARI)
    assert "front_after" not in out, "the front did not change again after the input"
    assert "no UI change" in out["note"] and "Keynote" in out["note"]


def test_a_target_that_did_not_come_forward_sends_nothing(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="right", app="Stuck"))
    assert out["ok"] is False and "observed_effect" not in out, out
    assert out["reason"] == "not_frontmost_after_activate" and "Safari" in out["error"] and "nothing was sent" in out["error"]
    assert out["target"]["verified"] is False and inputs(state) == [], "the guard fires: no cliclick at all"


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
    assert out["delivered_to"]["app"] == "Safari" and "target" not in out and "front_before" not in out
    # app="" is what a voice model sends for an unused optional field: identical to omitting it.
    state["calls"].clear()
    out2 = receipt(api.tools["key"](keys="right", app=""))
    assert out2["delivered_to"]["app"] == "Safari" and state["helper"] == []


def test_without_lsappinfo_the_place_is_unknown_and_the_input_still_goes(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch, lsappinfo=False)
    out = receipt(api.tools["key"](keys="return"))
    assert out["ok"] is True and out["observed_effect"] is True
    assert out["delivered_to"] == "unknown" and "unknown" in out["note"] and "front_after" not in out
    assert inputs(state) == [["cliclick", "kp:return"]]


def test_a_front_change_after_the_input_is_reported(tmp_path, monkeypatch):
    def switch(argv, state):
        state["front"] = KEYNOTE  # cmd+tab moved the front
    module, api, state = mac(tmp_path, monkeypatch, on_input=switch)
    out = receipt(api.tools["key"](keys="cmd+tab"))
    assert out["delivered_to"]["app"] == "Safari" and out["front_after"]["app"] == "Keynote", out


def test_a_dropped_input_is_an_honest_failure(tmp_path, monkeypatch):
    """cliclick exits 0 and only warns on stderr when macOS has no Accessibility grant for the posting process."""
    warning = "WARNING: Accessibility privileges not enabled. Many actions may fail.\n"
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(0, "", warning))
    out = receipt(api.tools["key"](keys="right"))
    assert out["ok"] is False and "observed_effect" not in out, out
    assert out["reason"] == "permission_denied" and out["permission"] == {"accessibility": "untrusted", "source": "helper stderr"}
    assert "Accessibility" in out["helper_stderr"]
    # A switch observed before the dropped input keeps its own mark.
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "permission_denied" and out["observed_effect"] is True and out["activation_done"] is True
    # Other stderr noise is kept beside a successful dispatch.
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(0, "", "some note\n"))
    out = receipt(api.tools["key"](keys="right"))
    assert out["ok"] is True and out["observed_effect"] is True and out["helper_stderr"] == "some note"


def test_characters_the_layout_cannot_type_are_an_honest_failure(tmp_path, monkeypatch):
    """Measured on the live probe (27.10.2026): under a Cyrillic layout cliclick typed none of 'ouroboros probe',
    printed one 'Unable to get key code for <c>' line per letter and exited 0. The hint names the method used."""
    stderr = "".join(f"Unable to get key code for {c} (see README-Characters.md)\n" for c in "ourbs")
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(0, "", stderr))
    out = receipt(api.tools["type_text"](text="ouroboros probe"))
    assert out["ok"] is False and "observed_effect" not in out, out
    assert out["reason"] == "untypable_characters" and out["untyped"] == list("ourbs") and "via=paste" in out["error"]
    shortcut = receipt(api.tools["key"](keys="cmd+s"))
    assert shortcut["reason"] == "untypable_characters" and "layout" in shortcut["error"] and "via=paste" not in shortcut["error"]
    pasted = receipt(api.tools["type_text"](text="x", via="paste"))
    assert pasted["reason"] == "untypable_characters" and "paste chord" in pasted["error"] and "via=paste" not in pasted["error"]
    module, api, state = mac(tmp_path, monkeypatch)
    assert receipt(api.tools["type_text"](text="ouroboros probe"))["observed_effect"] is True, "a clean dispatch keeps the mark"


def test_an_activation_that_ran_before_a_failed_input_keeps_its_mark_and_says_the_input_is_unknown(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch, cliclick=(1, "", "boom"))
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["observed_effect"] is True and out["activation_done"] is True, out
    assert out["target"]["activated"] is True and out["error"] == "boom"
    assert "delivered_to" not in out and out["front_at_dispatch"]["app"] == "Keynote"
    assert "unknown" in out["note"] and "dispatched while" not in out["note"] and "switch to Keynote" in out["note"]


# --- key sequences: one switch, every later step re-checked, every recipient named ---------------

def test_a_key_sequence_brings_the_target_forward_once_and_rechecks_before_each_step(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="down down return", app="Keynote"))
    assert out["ok"] is True and out["steps"] == 3 and out["observed_effect"] is True
    assert [args[0] for args in state["helper"]] == ["activate"], "one activation for the whole sequence"
    assert len(inputs(state)) == 3 and out["target"]["activated"] is True and out["delivered_to"]["app"] == "Keynote"
    assert "steps_delivered_to" not in out and "no UI change" in out["note"]
    reads = [argv for argv in state["calls"] if argv[:2] == ["lsappinfo", "front"]]
    assert len(reads) >= 2, "the front is read again before the later steps"


def test_a_targeted_sequence_stops_when_its_app_leaves_the_front(tmp_path, monkeypatch):
    """The 27.10 shape inside one call: cmd+tab (sent to Keynote) moves the front to Safari; the arrow is NOT sent."""
    def switch(argv, state):
        if "kp:tab" in argv:
            state["front"] = SAFARI
    module, api, state = mac(tmp_path, monkeypatch, on_input=switch)
    out = receipt(api.tools["key"](keys="cmd+tab right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "target_lost" and out["steps_done"] == 1 and out["steps_total"] == 2, out
    assert inputs(state) == [["cliclick", "kd:cmd", "kp:tab", "ku:cmd"]], "the arrow was never sent"
    assert out["observed_effect"] is True, "the first step ran"
    assert out["delivered_to"]["app"] == "Keynote" and out["front"]["app"] == "Safari" and out["front_after"]["app"] == "Safari"
    assert "no longer in front" in out["error"] and "nothing more was sent" in out["error"]


def test_an_untargeted_sequence_names_every_recipient_when_they_differ(tmp_path, monkeypatch):
    def switch(argv, state):
        if "kp:tab" in argv:
            state["front"] = KEYNOTE
    module, api, state = mac(tmp_path, monkeypatch, on_input=switch)
    out = receipt(api.tools["key"](keys="cmd+tab right"))
    assert out["ok"] is True and out["steps"] == 2 and len(inputs(state)) == 2
    assert out["delivered_to"]["app"] == "Safari" and out["front_after"]["app"] == "Keynote"
    assert [r["app"] for r in out["steps_delivered_to"]] == ["Safari", "Keynote"], out


def test_a_sequence_refused_at_its_first_step_carries_the_refusal(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="down return", app="Term"))
    assert out["ok"] is False and out["reason"] == "ambiguous" and [c["app"] for c in out["candidates"]] == ["Terminal", "Termius"]
    assert inputs(state) == [] and "observed_effect" not in out


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
    assert again["target"]["already_front"] is True and again["target"]["verified"] is True
    refused = receipt(api.tools["activate_app"](app="Stuck"))
    assert refused["ok"] is False and "observed_effect" not in refused and refused["reason"] == "not_frontmost_after_activate"
    assert receipt(api.tools["activate_app"](app=""))["ok"] is False


def test_activate_app_does_not_claim_a_switch_when_the_previous_front_was_unknown(tmp_path, monkeypatch):
    def helper(args, state):
        return {"ok": True, "target": app_dict(KEYNOTE), "front_before": None, "front": app_dict(KEYNOTE),
                "activated": None, "verified": True, "method": "ax_frontmost", "waited_ms": 90}
    module, api, state = mac(tmp_path, monkeypatch, helper=helper)
    out = receipt(api.tools["activate_app"](app="Keynote"))
    assert out["ok"] is True and out["activated"] is None and "observed_effect" not in out and "unknown" in out["note"]
    sent = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert sent["ok"] is True and sent["observed_effect"] is True and sent["target"]["verified"] is True, "verified in front: the input goes"


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
    assert caps["frontmost"]["app"] == "Safari" and caps["targeting"]["app"] == "supported"
    module, api, state = mac(tmp_path, monkeypatch, helper=lambda args, st: {"ok": False, "error": "no answer"})
    caps = receipt(api.tools["capabilities"]())
    assert caps["permission_state_verified"] is False and "error" in caps["permissions"] and caps["targeting"]["app"] == "unavailable"


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

def test_type_text_via_paste_writes_the_clipboard_after_the_target_is_verified(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    out = receipt(api.tools["type_text"](text="Привет, мир " * 40, via="paste", app="Keynote"))
    assert out["ok"] is True and out["method"] == "paste" and out["clipboard_replaced"] is True
    assert state["clipboard"].startswith("Привет") and inputs(state) == [["cliclick", "kd:cmd", "t:v", "ku:cmd"]]
    assert out["delivered_to"]["app"] == "Keynote"
    activate_at = next(i for i, argv in enumerate(state["calls"]) if any(p.endswith("macos_native.py") for p in argv))
    copy_at = next(i for i, argv in enumerate(state["calls"]) if argv[0] == "pbcopy")
    assert activate_at < copy_at, "the clipboard is written only after the target is verified in front"
    plain = receipt(api.tools["type_text"](text="hi"))
    assert plain["ok"] is True and inputs(state)[-1] == ["cliclick", "t:hi"] and "method" not in plain
    # A refusal leaves the owner's clipboard alone and says nothing was sent.
    module, api, state = mac(tmp_path, monkeypatch)
    refused = receipt(api.tools["type_text"](text="secret", via="paste", app="NoSuchApp"))
    assert refused["ok"] is False and state["clipboard"] is None and inputs(state) == []
    assert receipt(api.tools["type_text"](text="x", via="clipboard"))["ok"] is False, "an unknown via is refused, not typed"


def test_screenshot_transform_uses_display_bounds_and_the_target_display(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_png_dimensions", lambda _p: (3456, 2234))
    out = receipt(api.tools["screenshot"](job_id="main"))
    assert out["ok"] is True and out["display"]["index"] == 1 and out["display"]["selected_by"] == "default"
    tf = out["coord_transform"]
    assert (tf["input_w"], tf["input_h"], tf["ox"], tf["oy"]) == (1728, 1117, 0, 0) and tf["approx"] is False
    assert inputs(state) == [] and ["screencapture", "-x", out["full_resolution_path"]] in state["calls"]
    # app= : the display holding that app's window is captured and the transform carries its origin.
    out = receipt(api.tools["screenshot"](job_id="deck", app="Keynote"))
    assert out["display"]["index"] == 2 and out["display"]["selected_by"] == "app" and out["app"]["pid"] == 200
    assert any(argv[:4] == ["screencapture", "-x", "-D", "2"] for argv in state["calls"])
    assert (out["coord_transform"]["ox"], out["coord_transform"]["oy"]) == (1728, 0)
    click = receipt(api.tools["click"](x=100, y=50))
    assert click["ok"] is True
    sx = out["coord_transform"]["sx"]
    assert inputs(state)[-1] == ["cliclick", f"c:{round(100 * sx) + 1728},{round(50 * out['coord_transform']['sy'])}"]


def test_screenshot_with_an_unlocatable_window_says_it_captured_the_main_display(tmp_path, monkeypatch):
    def helper(args, state):
        if args[0] == "tree":
            return {"ok": False, "reason": "accessibility_untrusted", "pid": int(args[1])}
        if args[0] == "resolve":
            return {"ok": True, "candidates": [app_dict(KEYNOTE)]}
        if args[0] == "displays":
            return {"ok": True, "displays": [{"id": 1, "main": True, "x": 0, "y": 0, "width": 1728, "height": 1117, "index": 1}]}
        return {"ok": False, "error": "no fake"}
    module, api, state = mac(tmp_path, monkeypatch, helper=helper)
    monkeypatch.setattr(module, "_png_dimensions", lambda _p: (3456, 2234))
    out = receipt(api.tools["screenshot"](job_id="fallback", app="Keynote"))
    assert out["ok"] is True and "app" not in out, "the main display is not presented as the named app's"
    assert out["display"]["selected_by"] == "fallback" and "accessibility_untrusted" in out["display"]["app_window_unread"]


def test_screenshot_falls_back_to_the_old_transform_when_displays_are_unreadable(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch, helper=lambda args, st: {"ok": False, "error": "locked"})
    monkeypatch.setattr(module, "_png_dimensions", lambda _p: (800, 600))
    monkeypatch.setattr(module, "_macos_logical_size", lambda: (0, 0))
    out = receipt(api.tools["screenshot"](job_id="fallback"))
    assert out["ok"] is True and "display" not in out and out["coord_transform"]["approx"] is True
    # A clean-looking Finder ratio is still approximate: the desktop size is the union of all displays.
    monkeypatch.setattr(module, "_macos_logical_size", lambda: (400, 300))
    out = receipt(api.tools["screenshot"](job_id="finder"))
    assert out["coord_transform"]["approx"] is True and out["coord_transform"]["input_w"] == 400


# --- registration -------------------------------------------------------------------------------

def test_the_surface_is_24_tools_with_app_on_the_inputs(tmp_path, monkeypatch):
    module, api, state = mac(tmp_path, monkeypatch)
    assert "activate_app" in api.tools and "clear_active_connection" not in api.tools
    assert len(api.tools) == 24
    for tool in ("click", "double_click", "triple_click", "left_click_drag", "mouse_down", "type_text", "key", "hold_key", "scroll"):
        assert api.meta[tool]["schema"]["properties"]["app"] == {"type": "string", "default": ""}, tool
    lead = api.meta["key"]["description"]
    assert lead.startswith("Press keys in the frontmost app, or in app=") and "not that the app reacted" in lead[:200]
    assert "run_command" in lead
    for tool in ("click", "double_click", "triple_click", "left_click_drag", "mouse_down", "hold_key", "scroll"):
        assert "app=" in api.meta[tool]["description"][:130], tool
    assert all(api.meta[tool]["timeout_sec"] >= 45 for tool in ("click", "key", "type_text", "activate_app")), "a switch plus an input fits the budget"


# --- Linux X11 ----------------------------------------------------------------------------------

WMCTRL = ("0x00000001  0 4242 10 20 800 600 navigator.Firefox host Browser - Docs\n"
          "0x00000002  0 5151 0 0 1024 768 keynote.Keynote host Deck\n"
          "0x00000003  0 5151 0 0 300 200 keynote.Keynote host Inspector\n")
TITLES = {"1": "Browser - Docs", "2": "Deck", "3": "Inspector"}
PIDS = {"1": "4242", "2": "5151", "3": "5151"}


def x11(tmp_path, monkeypatch, *, active="1", xdotool=True, switch_to=None, wmctrl=True):
    """A faked X11 desktop: ``xdotool getactivewindow`` prints the active window id (decimal), wmctrl the rows."""
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_session_type", lambda: "x11")
    tools = ({"wmctrl"} if wmctrl else set()) | ({"xdotool"} if xdotool else set())
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
        if argv == ["xdotool", "getactivewindow"]:
            return subprocess.CompletedProcess(argv, 0 if state["active"] else 1, (state["active"] + "\n") if state["active"] else "", "")
        if argv[:2] == ["xdotool", "getwindowpid"]:
            return subprocess.CompletedProcess(argv, 0, PIDS.get(argv[2], "") + "\n", "")
        if argv[:2] == ["xdotool", "getwindowname"]:
            return subprocess.CompletedProcess(argv, 0, TITLES.get(argv[2], "") + "\n", "")
        assert argv[:2] != ["xdotool", "getwindowclassname"], "the reader must not depend on a command old xdotool builds lack"
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    api = API(tmp_path / "state")
    module.register(api)
    return module, api, state


def test_x11_targeted_key_activates_through_wmctrl_and_verifies_with_xdotool(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch, switch_to="2")
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    assert ["wmctrl", "-i", "-a", "0x00000002"] in state["calls"]
    assert [argv for argv in state["calls"] if argv[:2] == ["xdotool", "key"]] == [["xdotool", "key", "Right"]]
    assert out["target"]["activated"] is True and out["target"]["verified"] is True
    assert out["delivered_to"] == {"window_id": "0x00000002", "pid": 5151, "app": "keynote.Keynote", "title": "Deck"}
    assert out["front_before"]["pid"] == 4242
    module, api, state = x11(tmp_path, monkeypatch)  # Firefox is already in front: no switch, one key
    already = receipt(api.tools["key"](keys="right", app="Firefox"))
    assert already["ok"] is True and already["target"]["activated"] is False and already["target"]["already_front"] is True
    assert not any(argv[:3] == ["wmctrl", "-i", "-a"] for argv in state["calls"])


def test_x11_refuses_when_the_switch_cannot_be_verified_or_matched(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch)  # wmctrl runs, xdotool keeps answering Firefox
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "not_frontmost_after_activate" and "observed_effect" not in out
    assert not any(argv[:2] == ["xdotool", "key"] for argv in state["calls"])
    missing = receipt(api.tools["key"](keys="right", app="Emacs"))
    assert missing["ok"] is False and missing["reason"] == "not_running" and "keynote.Keynote" in missing["running"]


def test_x11_sends_nothing_when_the_front_cannot_be_read_after_the_switch(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch, switch_to="")  # the switch ran; xdotool then names no window
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "front_unread" and "observed_effect" not in out, out
    assert not any(argv[:2] == ["xdotool", "key"] for argv in state["calls"]) and "delivered_to" not in out
    assert out["target"]["activated"] is None and out["target"]["verified"] is False
    unread = receipt(api.tools["activate_app"](app="Keynote"))
    assert unread["ok"] is False and unread["reason"] == "front_unread" and "front" not in unread and "observed_effect" not in unread


def test_x11_matches_a_class_or_title_exactly_and_never_a_substring(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch)
    out = receipt(api.tools["key"](keys="right", app="Docs"))  # only Firefox's TITLE contains it
    assert out["ok"] is False and out["reason"] == "not_running" and "observed_effect" not in out
    assert out["candidates"][0]["app"] == "navigator.Firefox" and "exact" in out["error"]
    assert not any(argv[:2] == ["xdotool", "key"] for argv in state["calls"])
    by_title = receipt(api.tools["key"](keys="right", app="Browser - Docs"))
    assert by_title["ok"] is True and by_title["target"]["resolved"]["matched_by"] == "title"
    # Two windows of one application are one target by class; two windows named by title are distinct.
    module, api, state = x11(tmp_path, monkeypatch, switch_to="3")
    inspector = receipt(api.tools["key"](keys="right", app="Inspector"))
    assert inspector["ok"] is True and inspector["delivered_to"]["window_id"] == "0x00000003"


def test_x11_without_xdotool_refuses_a_target_and_lists_windows_without_a_front(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch, xdotool=False)
    out = receipt(api.tools["type_text"](text="hi", app="Keynote"))
    assert out["ok"] is False and "backend" in out["error"], "typing on X11 needs xdotool: an honest backend error"
    switch = receipt(api.tools["activate_app"](app="Keynote"))
    assert switch["ok"] is False and switch["reason"] == "unsupported" and "xdotool" in switch["error"]
    rows = receipt(api.tools["window_list"]())
    assert rows["ok"] is True and rows["frontmost"] is None and "xdotool" in rows["front_unread"]
    assert all("frontmost" not in r for r in rows["windows"]), "an unread front is not rendered as false"
    caps = receipt(api.tools["capabilities"]())
    assert caps["targeting"]["app"] == "unsupported" and "xdotool" in caps["targeting"]["note"]


def test_x11_window_list_rows_and_wayland_refusal(tmp_path, monkeypatch):
    module, api, state = x11(tmp_path, monkeypatch)
    out = receipt(api.tools["window_list"]())
    assert out["ok"] is True and out["frontmost"]["pid"] == 4242 and out["frontmost"]["app"] == "navigator.Firefox"
    assert [(r["app"], r["frontmost"]) for r in out["windows"]] == [("navigator.Firefox", True), ("keynote.Keynote", False), ("keynote.Keynote", False)]
    assert receipt(api.tools["capabilities"]())["targeting"]["app"] == "supported"
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
    one_line = native.parse_info('"pid"=100 "CFBundleIdentifier"="com.google.Chrome" "LSDisplayName"="Google Chrome" ')
    assert one_line == {"pid": 100, "app_id": "com.google.Chrome", "app": "Google Chrome"}, "spaces between fields, names with spaces"
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
    assert native.resolve("900", apps) == [], "bare digits are a name, not a pid (pid:<n> is the form)"


def test_native_walk_is_bounded_and_says_which_bound_cut_it():
    from skills.unix_computer_use import macos_native as native

    graph = {"root": ["a", "b", "web"], "a": [f"a{i}" for i in range(50)], "b": [], "web": ["w1", "w2"]}

    def read(el):
        role = "AXWebArea" if el == "web" else ("AXWindow" if el == "root" else "AXButton")
        return 0, [role, None, el, None, None, (10, 20), (30, 40)]

    def children(el, n):
        kids = list(graph.get(el, []))
        return 0, kids[:n], len(kids)

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


def test_native_walk_names_a_wide_node_it_could_not_finish_and_releases_every_element_once():
    """A node wider than the node budget is cut by max_nodes and says how many children were not read (no
    hidden per-node cap); every fetched element is released exactly once, even when a read raises."""
    from skills.unix_computer_use import macos_native as native

    wide = {"root": ["list"], "list": [f"row{i}" for i in range(200)]}

    def read(el):
        return 0, ["AXRow" if el.startswith("row") else "AXGroup", None, el, None, None, (0, 0), (10, 10)]

    requested = []

    def children(el, n):
        requested.append((el, n))
        kids = list(wide.get(el, []))
        return 0, kids[:n], len(kids)

    released = []
    out = native.walk("root", read, children, max_nodes=50, deadline_s=10, release=released.append)
    assert out["visited"] == 50 and out["truncated_by"] == ["max_nodes"]
    assert requested[:2] == [("root", 50), ("list", 49)], "children are requested up to the remaining budget plus one"
    assert out["nodes"][1]["children_unread"] == 151
    assert sorted(released) == sorted(["root", "list"] + [f"row{i}" for i in range(49)]) and len(released) == len(set(released))

    def raising(el):
        if el == "list":
            raise RuntimeError("provider went away")
        return read(el)
    released.clear()
    with pytest.raises(RuntimeError):
        native.walk("root", raising, children, max_nodes=50, deadline_s=10, release=released.append)
    assert released == ["root", "list"], "the root and the element being read are released once each"
    assert native._bounds((float("nan"), 1.0), (2.0, 3.0)) is None and native._bounds((1.0, 2.0), (3.5, 4.5)) == [1, 2, 3, 4]


# --- remote targets: OSWorld guest and a Mac over SSH ------------------------------------------

GUEST_ROWS = ("0x00000001  0 4242 10 20 800 600 navigator.Firefox vm Browser\n"
              "0x00000002  0 5151 0 0 1024 768 keynote.Keynote vm Deck - Keynote\n"
              "0x00000003  0 5151 0 0 300 200 keynote.Keynote vm Inspector - Keynote\n")
FIREFOX_FRONT = "1|4242|Browser"
KEYNOTE_FRONT = "2|5151|Deck - Keynote"


class Guest:
    """``urlopen`` toward an OSWorld guest; ``answer(command)`` -> (returncode, output) per /execute call."""

    def __init__(self, answer):
        self.answer, self.commands = answer, []

    def __call__(self, request, timeout=None):
        command = json.loads(request.data.decode("utf-8"))["command"]
        self.commands.append(command)
        returncode, output = self.answer(command[-1], len(self.commands))
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


def osworld(tmp_path, monkeypatch, answer):
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_session_type", lambda: "x11")
    monkeypatch.setattr(module, "_which", lambda name: "")
    api = API(tmp_path / "state")
    module.register(api)
    api.tools["add_connection"](name="vm", backend="osworld_http", target="http://guest.invalid:5000", activate=True)
    guest = Guest(answer)
    monkeypatch.setattr(urllib.request, "urlopen", guest)
    return module, api, guest


def test_osworld_targeted_input_resolves_from_the_guest_rows_activates_by_window_id_and_reads_the_front(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, GUEST_ROWS + "__ACTIVE__" + FIREFOX_FRONT + "\n"
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 0, f"__FRONT_BEFORE__{KEYNOTE_FRONT}\n__FRONT_AFTER__{KEYNOTE_FRONT}\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    listing, activate, action = guest.commands
    assert listing[:2] == ["bash", "-lc"] and "wmctrl -lpxG" in listing[2]
    assert "wmctrl -i -a 0x00000002" in activate[2] and "Keynote" not in activate[2], "activation by window id, never by name"
    assert "pyautogui.press('right')" in action[2] and "__TARGET_LOST__" in action[2] and "'5151'" in action[2], "the guest checks the pid before pressing"
    assert out["target"]["activated"] is True and out["target"]["resolved"]["matched_by"] == "class"
    assert out["delivered_to"] == {"window_id": "0x00000002", "pid": 5151, "title": "Deck - Keynote"}
    assert out["front_before"] == {"window_id": "0x00000001", "pid": 4242, "title": "Browser", "app": "navigator.Firefox"}
    assert "__FRONT" not in json.dumps(out["execute_result"]), "the markers never reach the model as command output"


def test_osworld_refuses_when_the_guest_window_did_not_come_forward_or_does_not_exist(tmp_path, monkeypatch):
    def stays(code, index):
        if "wmctrl -lpxG" in code:
            return 0, GUEST_ROWS + "__ACTIVE__" + FIREFOX_FRONT + "\n"
        return 0, "__FRONT_AFTER__" + FIREFOX_FRONT + "\n"
    module, api, guest = osworld(tmp_path, monkeypatch, stays)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "not_frontmost_after_activate" and "observed_effect" not in out
    assert len(guest.commands) == 2, "the action was never sent"
    out = receipt(api.tools["click"](x=1, y=2, raw=True, app="Emacs"))
    assert out["ok"] is False and out["reason"] == "not_running" and len(guest.commands) == 3, "no activation for an absent window"
    hinted = receipt(api.tools["click"](x=1, y=2, raw=True, app="Deck"))
    assert hinted["ok"] is False and hinted["reason"] == "not_running" and hinted["candidates"][0]["title"] == "Deck - Keynote"


def test_osworld_already_front_and_same_process_windows(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, GUEST_ROWS + "__ACTIVE__" + KEYNOTE_FRONT + "\n"
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__3|5151|Inspector - Keynote\n"
        return 0, f"__FRONT_BEFORE__{KEYNOTE_FRONT}\n__FRONT_AFTER__{KEYNOTE_FRONT}\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["target"]["already_front"] is True and len(guest.commands) == 2, "no activation when already in front"
    switch = receipt(api.tools["activate_app"](app="Inspector - Keynote"))
    assert switch["ok"] is True and switch["activated"] is True and switch["observed_effect"] is True, switch
    assert switch["front"]["window_id"] == "0x00000003", "a window named by title is that window, even in the same process"
    again = receipt(api.tools["activate_app"](app="Keynote"))
    assert again["ok"] is True and again["activated"] is False and "observed_effect" not in again, "already in front: no mark"


def test_osworld_untargeted_input_names_the_front_and_tolerates_a_silent_probe(tmp_path, monkeypatch):
    def answer(code, index):
        if "pyautogui" in code:
            return 0, f"__FRONT_BEFORE__{FIREFOX_FRONT}\n__FRONT_AFTER__{FIREFOX_FRONT}\n"
        return 0, GUEST_ROWS + "__ACTIVE__" + FIREFOX_FRONT + "\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="return"))
    assert out["ok"] is True and out["delivered_to"] == {"window_id": "0x00000001", "pid": 4242, "title": "Browser"} and "front_after" not in out
    assert len(guest.commands) == 1 and "pyautogui.press('enter')" in guest.commands[0][2], "no activation without app="
    assert "except Exception" in guest.commands[0][2], "the probe cannot raise: a probe failure is never a failed input"
    rows = receipt(api.tools["window_list"]())
    assert rows["ok"] is True and rows["frontmost"]["pid"] == 4242 and [r["frontmost"] for r in rows["windows"]] == [True, False, False]
    # No markers at all (an older guest image): the action's exit code still governs, the place is unknown.
    module, api, guest = osworld(tmp_path, monkeypatch, lambda code, index: (0, ""))
    out = receipt(api.tools["key"](keys="return"))
    assert out["ok"] is True and out["observed_effect"] is True and out["delivered_to"] == "unknown"
    empty = receipt(api.tools["window_list"]())
    assert empty["ok"] is True and empty["windows"] == [] and empty["frontmost"] is None and "front_unread" in empty


def test_osworld_guest_precheck_withholds_an_input_when_the_target_left_the_front(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, GUEST_ROWS + "__ACTIVE__" + FIREFOX_FRONT + "\n"
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 3, f"__FRONT_BEFORE__{FIREFOX_FRONT}\n__TARGET_LOST__\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "target_lost" and "observed_effect" not in out, out
    assert out["front"]["pid"] == 4242 and "nothing more was sent" in out["error"]


def test_osworld_window_list_failure_is_not_an_empty_desktop(tmp_path, monkeypatch):
    module, api, guest = osworld(tmp_path, monkeypatch, lambda code, index: (127, "bash: wmctrl: command not found"))
    rows = receipt(api.tools["window_list"]())
    assert rows["ok"] is False and rows["reason"] == "unsupported" and "wmctrl" in rows["error"]
    assert receipt(api.tools["key"](keys="right", app="Keynote"))["reason"] == "unsupported"


LISTING = (' 1) "Safari" ASN:0x0-0x1: \n    bundleID="com.apple.Safari"\n    bundle path="/Applications/Safari.app"\n    pid = 100 type="Foreground" flavor=3\n'
           ' 2) "Keynote" ASN:0x0-0x2: \n    bundleID="com.apple.iWork.Keynote"\n    bundle path="/Applications/Keynote.app"\n    pid = 200 type="Foreground" flavor=3\n'
           ' 3) "Google Chrome" ASN:0x0-0x3: \n    bundleID="com.google.Chrome"\n    bundle path="/Applications/Google Chrome.app"\n    pid = 300 type="Foreground" flavor=3\n'
           ' 4) "Don\'t Sleep" ASN:0x0-0x4: \n    bundleID="com.example.dontsleep"\n    bundle path="/Applications/Don\'t Sleep.app"\n    pid = 400 type="Foreground" flavor=3\n')


def info_line(triple) -> str:
    name, pid, bid = triple
    return f'"pid"={pid} "CFBundleIdentifier"="{bid}" "LSDisplayName"="{name}" '


def ssh(tmp_path, monkeypatch, script):
    """A Mac over SSH: ``script(command)`` -> (rc, stdout[, stderr]) for every ssh call."""
    module = load_plugin()
    monkeypatch.setattr(module, "_platform", lambda: "linux")
    monkeypatch.setattr(module, "_session_type", lambda: "x11")
    monkeypatch.setattr(module, "_which", lambda name: "")
    calls = []

    def run(cmd, *a, **k):
        argv = [str(part) for part in cmd]
        calls.append(argv)
        answer = script(argv[-1])
        rc, out, err = (answer + ("",))[:3]
        return subprocess.CompletedProcess(argv, rc, out, err)
    monkeypatch.setattr(module.subprocess, "run", run)
    api = API(tmp_path / "state")
    module.register(api)
    api.tools["add_connection"](name="mac", backend="ssh_macos", host="mac.invalid", user="owner", activate=True)
    return module, api, calls


def mac_over_ssh(front, after=None, input_stderr=""):
    """Scripts: the listing reports ``front``; an activation ends with ``after`` (or ``front``) in front."""
    def script(command):
        if "lsappinfo list" in command:
            return 0, LISTING + "__FRONT__" + info_line(front)
        if "System Events" in command:
            return 0, f"__FRONT_BEFORE__{info_line(front)}\n__FRONT_AFTER__{info_line(after or front)}\n"
        if "cliclick" in command:
            now = after or front
            return 0, f"__FRONT_BEFORE__{info_line(now)}\n__FRONT_AFTER__{info_line(now)}\n", input_stderr
        return 0, ""
    return script


def test_ssh_targeted_input_resolves_from_lsappinfo_and_activates_by_pid(tmp_path, monkeypatch):
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI, KEYNOTE))
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    assert "lsappinfo list" in calls[0][-1] and "unix id is 200" in calls[1][-1] and "to activate" not in calls[1][-1]
    assert "cliclick kp:arrow-right" in calls[2][-1] and '"$P" != "200"' in calls[2][-1], "the same ssh call checks the pid before the input"
    assert out["delivered_to"]["app"] == "Keynote" and out["target"]["activated"] is True and out["front_before"]["app"] == "Safari"
    assert out["output"] == "", "the probe markers never reach the model as command output"
    assert "error" not in out


def test_ssh_names_with_spaces_or_apostrophes_are_never_interpolated(tmp_path, monkeypatch):
    chrome = ("Google Chrome", 300, "com.google.Chrome")
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI, chrome))
    out = receipt(api.tools["key"](keys="right", app="Google Chrome"))
    assert out["ok"] is True and out["delivered_to"]["app"] == "Google Chrome" and out["target"]["verified"] is True, out
    sleepy = ("Don't Sleep", 400, "com.example.dontsleep")
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI, sleepy))
    out = receipt(api.tools["key"](keys="right", app="Don't Sleep"))
    assert out["ok"] is True and "unix id is 400" in calls[1][-1]
    assert all("Don't" not in argv[-1] for argv in calls), "the name itself never enters a remote command"


def test_ssh_refuses_a_not_running_app_before_any_activation(tmp_path, monkeypatch):
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI))
    out = receipt(api.tools["key"](keys="right", app="Xcode"))
    assert out["ok"] is False and out["reason"] == "not_running" and "Keynote" in out["running"]
    assert len(calls) == 1 and "System Events" not in calls[0][-1] and "to activate" not in calls[0][-1], "nothing is launched"
    stuck = receipt(api.tools["type_text"](text="x", app="Keynote"))
    assert stuck["ok"] is False and stuck["reason"] == "not_frontmost_after_activate" and "observed_effect" not in stuck
    assert not any("cliclick" in argv[-1] for argv in calls)


def test_ssh_already_front_sends_without_a_switch_and_marks_no_activation(tmp_path, monkeypatch):
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(KEYNOTE))
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["target"]["already_front"] is True and "front_before" not in out
    assert len(calls) == 2 and "cliclick" in calls[1][-1]
    again = receipt(api.tools["activate_app"](app="Keynote"))
    assert again["ok"] is True and again["activated"] is False and "observed_effect" not in again


def test_ssh_input_dropped_by_the_remote_mac_is_an_honest_failure(tmp_path, monkeypatch):
    warning = "WARNING: Accessibility privileges not enabled. Many actions may fail.\n"
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI, input_stderr=warning))
    out = receipt(api.tools["key"](keys="right"))
    assert out["ok"] is False and out["reason"] == "permission_denied" and "observed_effect" not in out, out


def test_ssh_paste_writes_the_remote_clipboard(tmp_path, monkeypatch):
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI))
    out = receipt(api.tools["type_text"](text="Привет 'мир'", via="paste"))
    assert out["ok"] is True and out["method"] == "paste" and out["clipboard_replaced"] is True
    assert "pbcopy" in calls[-1][-1] and "cliclick kd:cmd t:v ku:cmd" in calls[-1][-1]
