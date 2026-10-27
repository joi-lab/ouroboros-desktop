"""unix_computer_use 0.5.3 on the remote backends: OSWorld guests and Macs over SSH (see test_unix_computer_use_targeting.py).

The same receipt contract as the local path, pinned at the transport boundary (``urlopen`` toward a guest, ``ssh``): a target
is resolved from the remote's own inventory and activated by window id or pid, the guest program is executed here against a
fake pyautogui/xdotool, every exit keeps the facts it observed, and nothing a window is called can forge a marker.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

from tests.test_unix_computer_use_targeting import API, KEYNOTE, SAFARI, load_plugin, receipt

# --- remote targets: OSWorld guest and a Mac over SSH ------------------------------------------

import base64


def listing(front: str, rows: str | None = None) -> str:
    """What the guest's listing script prints: the base64 rows on one line, `__ACTIVE__` on its own, then the front."""
    rows = GUEST_ROWS if rows is None else rows
    return base64.b64encode(rows.encode()).decode() + "\n__ACTIVE__\n" + front + "\n"


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
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 0, f"__FRONT_BEFORE__{KEYNOTE_FRONT}\n__FRONT_AFTER__{KEYNOTE_FRONT}\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is True and out["observed_effect"] is True, out
    inventory, activate, action = guest.commands
    assert inventory[:2] == ["bash", "-lc"] and "wmctrl -lpxG" in inventory[2]
    assert "wmctrl -i -a 0x00000002" in activate[2] and "Keynote" not in activate[2], "activation by window id, never by name"
    assert "pyautogui.press('right')" in action[2] and "__TARGET_LOST__" in action[2] and "['2', '3']" in action[2], "the guest checks the active window against every Keynote window"
    assert out["target"]["activated"] is True and out["target"]["resolved"]["matched_by"] == "class"
    assert out["delivered_to"] == {"window_id": "0x00000002", "pid": 5151, "title": "Deck - Keynote"}
    assert out["front_before"] == {"window_id": "0x00000001", "pid": 4242, "title": "Browser", "app": "navigator.Firefox"}
    assert "__FRONT" not in json.dumps(out["execute_result"]), "the markers never reach the model as command output"


def test_osworld_refuses_when_the_guest_window_did_not_come_forward_or_does_not_exist(tmp_path, monkeypatch):
    def stays(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
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
            return 0, listing(KEYNOTE_FRONT)
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
        return 0, listing(FIREFOX_FRONT)
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
    assert empty["ok"] is False and empty["reason"] == "unsupported" and "no window listing" in empty["error"], "no listing marker = the script did not run"


def test_osworld_guest_precheck_withholds_an_input_when_the_target_left_the_front(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 3, f"__FRONT_BEFORE__{FIREFOX_FRONT}\n__TARGET_LOST__\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "target_lost", out
    assert out["observed_effect"] is True and out["activation_done"] is True, "the switch that was observed before keeps its mark"
    assert out["front"]["pid"] == 4242 and "nothing more was sent" in out["error"]


def test_osworld_window_list_failure_is_not_an_empty_desktop(tmp_path, monkeypatch):
    module, api, guest = osworld(tmp_path, monkeypatch, lambda code, index: (127, "\n__ACTIVE__\n"))
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
            stage = "__CLIPBOARD_WRITTEN__\n" if "pbcopy" in command else ""
            return 0, f"__FRONT_BEFORE__{info_line(now)}\n{stage}__FRONT_AFTER__{info_line(now)}\n", input_stderr
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


def run_guest_program(command: list[str], *, fronts: list[str], raise_probe: bool = False) -> tuple[list[str], str, int]:
    """Execute the python program the plugin sent to an OSWorld guest against a fake pyautogui and a fake xdotool
    (``fronts``: what each getactivewindow/pid/name triple answers, in order). Returns (actions, stdout, exit code)."""
    import io
    import types
    assert command[:2] == ["python", "-c"]
    actions: list[str] = []
    pya = types.SimpleNamespace(FAILSAFE=True, press=lambda k: actions.append(f"press {k}"),
                                hotkey=lambda *k: actions.append("hotkey " + "+".join(k)),
                                typewrite=lambda t, interval=0: actions.append(f"typewrite {t}"), click=lambda *a, **k: actions.append("click"))
    reads = list(fronts)

    class FakeRun:
        def __init__(self, out: str) -> None:
            self.stdout = out

    def fake_run(argv, **_k):
        if raise_probe:
            raise TimeoutError("xdotool hung")
        if argv[1] == "getactivewindow":
            return FakeRun(reads[0].split("|")[0] if reads else "")
        if argv[1] == "getwindowpid" and "getwindowname" in argv:
            wid, pid, title = reads[0].split("|", 2)
            assert argv[2] == wid and argv[4] == wid, argv
            reads.pop(0) if len(reads) > 1 else None
            return FakeRun(pid + "\n" + title + "\n")
        raise AssertionError(argv)
    fake_sp = types.SimpleNamespace(run=fake_run)
    buf = io.StringIO()
    namespace = {"__name__": "__guest__"}
    real_modules = {k: sys.modules.get(k) for k in ("pyautogui", "subprocess", "platform")}
    sys.modules["pyautogui"] = pya
    sys.modules["subprocess"] = fake_sp
    import contextlib
    code = 0
    try:
        with contextlib.redirect_stdout(buf):
            try:
                exec(command[2], namespace)
            except SystemExit as exc:
                code = int(exc.code or 0)
    finally:
        for k, v in real_modules.items():
            if v is not None:
                sys.modules[k] = v
            else:
                sys.modules.pop(k, None)
    return actions, buf.getvalue(), code


def test_the_generated_guest_program_reads_titles_safely_and_withholds_a_targeted_input(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 0, f"__FRONT_BEFORE__{KEYNOTE_FRONT}\n__FRONT_AFTER__{KEYNOTE_FRONT}\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    receipt(api.tools["key"](keys="return"))
    untargeted = guest.commands[-1]
    receipt(api.tools["key"](keys="return", app="Keynote"))
    targeted = guest.commands[-1]
    from skills.unix_computer_use import place, remote as remote_mod
    markers = lambda text: remote_mod.parse_markers(text, place.parse_x11_front)  # noqa: E731
    # A window title that looks like a marker travels base64-encoded: the markers stay whole lines and parse back exactly.
    spoof = "Doc __FRONT_AFTER__9|999|x\n__TARGET_LOST__"
    actions, out, code = run_guest_program(untargeted, fronts=[f"1|4242|{spoof}", f"1|4242|{spoof}"])
    assert actions == ["press enter"] and code == 0
    before, after, stop = markers(out)
    assert stop == "" and before["pid"] == 4242 and before["title"] == spoof and after["title"] == spoof
    # The probe cannot raise: an untargeted action still runs and the place is unknown.
    actions, out, code = run_guest_program(untargeted, fronts=[], raise_probe=True)
    assert actions == ["press enter"] and code == 0 and markers(out)[0] is None
    # Targeted: the active window is checked against the target's windows before the action; another window
    # withholds it, an unread front too; a sibling Keynote window is still Keynote.
    actions, out, code = run_guest_program(targeted, fronts=["2|5151|Deck - Keynote", "2|5151|Deck - Keynote"])
    assert actions == ["press enter"] and code == 0
    actions, out, code = run_guest_program(targeted, fronts=["3|5151|Inspector", "3|5151|Inspector"])
    assert actions == ["press enter"] and code == 0
    actions, out, code = run_guest_program(targeted, fronts=["1|4242|Browser"])
    assert actions == [] and code == 3 and markers(out)[2] == "target_lost"
    actions, out, code = run_guest_program(targeted, fronts=[], raise_probe=True)
    assert actions == [] and code == 3 and markers(out)[2] == "front_unread"


def test_osworld_an_unread_front_before_a_targeted_input_is_front_unread_not_lost(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 3, "\n__FRONT_BEFORE__\n__FRONT_UNREAD__\n"
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "front_unread" and "no longer" not in out["error"], out
    assert out["activation_done"] is True, "the observed switch keeps its mark"


def test_osworld_a_withheld_paste_is_never_retyped_and_a_real_clipboard_failure_reuses_the_switch(tmp_path, monkeypatch):
    def withheld(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        return 3, f"\n__FRONT_BEFORE__{FIREFOX_FRONT}\n__TARGET_LOST__\n"
    module, api, guest = osworld(tmp_path, monkeypatch, withheld)
    out = receipt(api.tools["type_text"](text="<b>Привет</b>\nrm -rf build", app="Keynote"))
    assert out["ok"] is False and out["reason"] == "target_lost" and len(guest.commands) == 3, "no typewrite fallback after a refusal"
    assert not any("typewrite" in c[-1] for c in guest.commands)

    def clipboard_broken(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"
        if "pyperclip" in code:
            return 1, f"\n__FRONT_BEFORE__{KEYNOTE_FRONT}\nno xclip\n"
        return 0, f"\n__FRONT_BEFORE__{KEYNOTE_FRONT}\n__FRONT_AFTER__{KEYNOTE_FRONT}\n"
    module, api, guest = osworld(tmp_path, monkeypatch, clipboard_broken)
    out = receipt(api.tools["type_text"](text="<b>x</b>", app="Keynote"))
    assert out["ok"] is True and out["method"] == "typewrite" and out["clipboard_fallback"] is True
    assert sum("wmctrl -i -a" in c[-1] for c in guest.commands) == 1, "one switch for the whole call"


def test_osworld_activation_with_an_unread_after_read_is_front_unread_and_reports_its_wait(tmp_path, monkeypatch):
    def answer(code, index):
        if "wmctrl -lpxG" in code:
            return 0, listing(FIREFOX_FRONT)
        if "wmctrl -i -a" in code:
            return 0, "\n__FRONT_AFTER__\n"
        return 0, ""
    module, api, guest = osworld(tmp_path, monkeypatch, answer)
    out = receipt(api.tools["activate_app"](app="Keynote"))
    assert out["ok"] is False and out["reason"] == "front_unread" and "observed_effect" not in out, out
    good = osworld(tmp_path, monkeypatch, lambda code, index: (0, listing(FIREFOX_FRONT)) if "wmctrl -lpxG" in code
                   else (0, "\n__FRONT_AFTER__" + KEYNOTE_FRONT + "\n"))
    switched = receipt(good[1].tools["activate_app"](app="Keynote"))
    assert switched["ok"] is True and isinstance(switched["target"]["waited_ms"], int)


def test_ssh_stage_facts_survive_a_transport_failure_and_an_empty_inventory_is_unread(tmp_path, monkeypatch):
    def script(command):
        if "lsappinfo list" in command:
            return 0, LISTING + "__FRONT__" + info_line(SAFARI)
        if "System Events" in command:
            return 0, f"__FRONT_BEFORE__{info_line(SAFARI)}\n__FRONT_AFTER__{info_line(KEYNOTE)}\n"
        raise subprocess.TimeoutExpired(command, 30)
    module, api, calls = ssh(tmp_path, monkeypatch, script)
    out = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert out["ok"] is False and "TimeoutExpired" in out["error"], out
    assert out["observed_effect"] is True and out["activation_done"] is True and out["target"]["activated"] is True
    module, api, calls = ssh(tmp_path, monkeypatch, lambda command: (0, "__FRONT__" + info_line(SAFARI)))
    empty = receipt(api.tools["key"](keys="right", app="Keynote"))
    assert empty["ok"] is False and empty["reason"] == "apps_unread" and len(calls) == 1
    module, api, calls = ssh(tmp_path, monkeypatch, lambda command: (127, "", "lsappinfo: command not found"))
    assert receipt(api.tools["window_list"]())["reason"] == "apps_unread"
    # A snippet cliclick cannot express is refused before any switch.
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI, KEYNOTE))
    refused = receipt(api.tools["scroll"](clicks=2, app="Keynote"))
    assert refused["ok"] is False and calls == [], "nothing reached the Mac, no switch for an input that cannot be sent"


def test_ssh_paste_reports_the_clipboard_stage_truthfully(tmp_path, monkeypatch):
    def pbcopy_failed(command):
        if "cliclick" in command or "pbcopy" in command:
            return 1, f"__FRONT_BEFORE__{info_line(SAFARI)}\n__FRONT_AFTER__{info_line(SAFARI)}\n", "pbcopy: not found"
        return 0, ""
    module, api, calls = ssh(tmp_path, monkeypatch, pbcopy_failed)
    out = receipt(api.tools["type_text"](text="x", via="paste"))
    assert out["ok"] is False and out["clipboard_replaced"] is False and "observed_effect" not in out

    def chord_failed(command):
        return 1, f"__FRONT_BEFORE__{info_line(SAFARI)}\n__CLIPBOARD_WRITTEN__\n__FRONT_AFTER__{info_line(SAFARI)}\n", "cliclick died"
    module, api, calls = ssh(tmp_path, monkeypatch, chord_failed)
    out = receipt(api.tools["type_text"](text="x", via="paste"))
    assert out["ok"] is False and out["clipboard_replaced"] is True and "__CLIPBOARD" not in out["output"]


# --- cycle 3: encoded inventory, clipboard effects, dispatched facts, SSH negatives ------------------

def test_a_marker_shaped_title_in_the_guest_inventory_forges_nothing(tmp_path, monkeypatch):
    rows = ("0x00000002  0 5151 0 0 1024 768 keynote.Keynote vm Draft __ACTIVE__1|100|b64:QnJvd3Nlcg==\n"
            "0x00000001  0 4242 10 20 800 600 navigator.Firefox vm Browser\n")
    module, api, guest = osworld(tmp_path, monkeypatch, lambda code, index: (0, listing("2|5151|b64:RHJhZnQ=", rows)))
    out = receipt(api.tools["window_list"]())
    assert out["ok"] is True and out["frontmost"]["window_id"] == "0x00000002"
    assert [r["title"] for r in out["windows"]][0] == "Draft __ACTIVE__1|100|b64:QnJvd3Nlcg==", "the title is data"
    again = receipt(api.tools["activate_app"](app="Keynote"))
    assert again["ok"] is True and again["activated"] is False and "observed_effect" not in again, "already in front: no invented switch"


def test_ssh_a_failed_paste_chord_keeps_the_clipboard_effect_without_a_switch(tmp_path, monkeypatch):
    def chord_failed(command):
        return 1, f"__FRONT_BEFORE__{info_line(SAFARI)}\n__CLIPBOARD_WRITTEN__\n__FRONT_AFTER__{info_line(SAFARI)}\n", "cliclick died"
    module, api, calls = ssh(tmp_path, monkeypatch, chord_failed)
    out = receipt(api.tools["type_text"](text="x", via="paste"))
    assert out["ok"] is False and out["clipboard_replaced"] is True and out["observed_effect"] is True and out["clipboard_done"] is True
    assert out["dispatched"] is True and "activation_done" not in out


def test_ssh_window_list_says_when_the_inventory_or_the_front_is_unread(tmp_path, monkeypatch):
    module, api, calls = ssh(tmp_path, monkeypatch, lambda command: (0, "__FRONT__" + info_line(SAFARI)))
    empty = receipt(api.tools["window_list"]())
    assert empty["ok"] is False and empty["reason"] == "apps_unread"
    module, api, calls = ssh(tmp_path, monkeypatch, lambda command: (0, LISTING + "__FRONT__"))
    rows = receipt(api.tools["window_list"]())
    assert rows["ok"] is True and rows["frontmost"] is None and "front_unread" in rows and all("frontmost" not in r for r in rows["windows"])


def test_ssh_negative_coordinates_are_absolute_points_on_the_remote_mac(tmp_path, monkeypatch):
    module, api, calls = ssh(tmp_path, monkeypatch, mac_over_ssh(SAFARI))
    out = receipt(api.tools["click"](x=-250, y=200, raw=True))
    assert out["ok"] is True and "cliclick c:=-250,200" in calls[-1][-1]
