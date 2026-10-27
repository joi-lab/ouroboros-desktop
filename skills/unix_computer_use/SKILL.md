---
name: unix_computer_use
description: Local and remote desktop observation/input tools with coordinate normalization, application targeting and honest place receipts (local macOS/Linux by default; optional OSWorld HTTP and SSH Mac backends).
version: 0.5.3
type: extension
entry: plugin.py
plugin_api: "2.0"
runtime: python3
os: [darwin, linux]
permissions: [tool, subprocess, net]
env_from_settings: []
tools:
  - name: window_list
    effect: read
  - name: ax_tree
    effect: read
  - name: capabilities
    effect: read
  - name: cursor_position
    effect: read
  - name: list_connections
    effect: read
when_to_use: The user asks Ouroboros to inspect a desktop, take a screenshot, act in a named application (click/type/press keys there), drag, or operate a local/explicitly configured remote GUI under human or benchmark supervision.
model_experience:
  what_model_sees: Two dozen desktop tools (screenshot, click/type/key/scroll input with an optional app= target, activate_app, window_list with the frontmost app, ax_tree as bounded text, capabilities with real grants, connection registry) join the tool list; each screenshot auto-attaches a WXGA-bounded image to the conversation in the same round, and every input answers with who was in front when it was dispatched.
  token_effect: The tool schemas add a moderate fixed cost to every round while the skill is enabled; each screenshot adds one image attachment (the dominant per-use cost), each input receipt about a hundred tokens of place facts, and an ax_tree read grows with the nodes it returns (bounded by max_nodes).
---

# Unix Computer Use

This bundled extension exposes a reviewable computer-use substrate for local
macOS/Linux desktops and explicitly configured remote targets. **All actions are
for supervised, low-risk workflows under human or benchmark observation.** It is
offered and on by default on macOS and Linux (`os`); Windows desktops use
`windows_computer_use`. A previously enabled Unix remote skill stays available
on Windows through later OFF/ON. Actor-less legacy ON is retained too: manual
and early automatic choices cannot be distinguished. This adds no local Windows backend.

Tools:

- `capabilities` reports the platform, display-session type, who is in front,
  the OS grants this process holds (prompt-free preflights on macOS:
  Accessibility, Screen Recording, input events; `permission_state_verified`
  is true only when they ran), whether `app=` targeting works in this session,
  and the detected screenshot/input backends plus the active connection.
- `list_connections`, `add_connection`, `test_connection`, `activate_connection`,
  and `use_local` manage the local/remote connection registry in skill state.
- `screenshot` captures the main display (or, with `app=`, the display holding
  that application's front window), downscales the image to fit WXGA
  (1280x800, configurable) and persists the exact image-to-input coordinate
  transform, including the display's origin. Pass coordinates read off the
  returned image directly to the input tools — they are remapped
  automatically. The result carries the typed `auto_attach_image` field, so
  the host attaches the image to the conversation in the same round; an engine
  that cannot take images receives the path instead (host behaviour), so the
  text facts in every other tool are the way to check without a picture.
- `click` (left/right/middle, double/triple), the thin aliases
  `double_click`/`triple_click` (models call these names unprompted), `move`,
  `left_click_drag`, `mouse_down`/`mouse_up`, `cursor_position`, `type_text`,
  `key`, `hold_key`, `scroll`, and `wait` execute input through platform tools
  when available. Pointer coordinates pass through one normalizer that accepts
  the malformations models actually emit (the pair packed into `x` with `y`
  absent or duplicated) and fails loudly on anything ambiguous.
- `activate_app` brings a running application to the front (name, bundle id or
  `pid:<n>` as `window_list` prints it; it never launches anything) and reports
  who is in front afterwards.
- `window_list` lists running applications with their windows: who is in
  front, titles, bounds, pid, and on macOS whether the application has its own
  scripting commands (`scriptable`); `app=` filters, `limit` caps the rows.
- `ax_tree` reads one window as text through the Accessibility API, bounded:
  the frontmost application's window, or `app=`'s without bringing it forward.
- `remote_exec` runs a shell command on the active remote backend only; it
  refuses on local backend. Use ordinary Ouroboros shell/file tools for local
  work.

Acting in a named application (`app=`):

- `click`, `double_click`, `triple_click`, `left_click_drag`, `mouse_down`,
  `type_text`, `key`, `hold_key` and `scroll` take an optional `app`: a running
  application's name (case-insensitive), its bundle id or WM_CLASS, or
  `pid:<n>` copied from `window_list`. The tool resolves it without launching
  anything, brings it forward when it is not already in front, reads who is in
  front again, and only then sends the input. Two matches are a refusal that
  lists the candidates; an application that is not running, or that did not
  come to the front (macOS 14+ may refuse activation from a background
  process), is a refusal that names who is in front instead. A refusal sends
  nothing and carries no effect mark. An empty `app` (what a voice model sends
  for an unused optional field) means "untargeted": the input goes to whatever
  is in front, exactly as before.
- `move` and `mouse_up` never change which application is in front: a drag
  must end where it started.
- Keyboard focus follows the switch and stays there, as if the owner had
  clicked the application; nothing moves it back. Do this only when the owner
  asked for an action in that application (owner decision 27.10.2026).
- Where an application has its own commands (macOS: `scriptable: true` in
  `window_list`; AppleScript/JXA through `run_command`/`run_script`, e.g.
  `osascript -e 'tell application id "com.apple.iWork.Keynote" to show next'`
  while a presentation plays, or `set current slide of front document to
  slide 2` in the editor), prefer them: they do not depend on which window is
  in front and they can read the state back (`slide number of current slide`).
  A keystroke is the right instrument only where no such command exists, and
  the key that means "next" differs per application and per mode (Keynote
  advances on Right Arrow while presenting, Down Arrow in the editor's
  navigator).

Coordinate contract:

- Input tools accept coordinates in the LAST screenshot's image space and
  remap them through the stored transform (scale and display origin);
  `raw=true` bypasses remapping.
- macOS input consumes LOGICAL points while screenshots are physical pixels;
  the stored transform folds the Retina scale in, computed from the captured
  display's own CoreGraphics bounds (a second display no longer stretches the
  scale). Without those bounds the Finder desktop size is used and the
  transform is flagged `approx`.

Input receipts:

- A successful input call returns `observed_effect: true`: the input command
  ran and reported success, on every backend. It proves dispatch, not that the
  screen changed or that the task's goal was met; the host's task result
  review reads it as work the task produced.
- Beside it every input receipt carries the place: `delivered_to` (the
  application in front when the input was dispatched: app, pid, bundle id or
  class; `"unknown"` when no reader is available), `front_before` (only when
  the tool switched applications first), `front_after` (only when the front
  changed again after the input, e.g. after `cmd+tab`), `target` (with `app=`:
  what was requested and resolved, whether it was activated and verified, how
  long the switch took), `helper_stderr` when the input helper printed
  anything, and one `note` saying that no UI change was observed by this tool.
- On macOS, `cliclick` exits 0 and only warns on stderr when the posting
  process lacks the Accessibility grant and the OS drops the event. Such a
  receipt is `ok: false` with `reason: permission_denied` and no mark.
- A switch that happened before a failed input keeps the mark
  (`activation_done`): the front changed, which is an observed effect.
- An input of several steps that fails after earlier steps succeeded returns
  `ok: false` with `steps_done` and keeps the mark. A failed or timed-out first
  step, a refusal, reads, `wait` and `remote_exec` carry none. `activate_app`
  carries the mark only when the front actually changed to the target.

Observation without images (`ax_tree`, `window_list`):

- macOS: a pure-ctypes Accessibility client runs in a short-lived helper
  process (`macos_native.py`), so a stuck application can be cut off and a
  native fault never reaches the host. Each node is read in one multi-attribute
  call with a per-application messaging timeout; the walk is breadth-first
  from the focused window (`root=focused` starts at the focused element),
  stops at `max_nodes` (default 150, ceiling 2000) or `timeout_ms` (default
  3000, ceiling 15000) and names every cut in `truncated_by`; web content
  (`AXWebArea`) is skipped unless `include_web=true`. Nodes carry role, label,
  value (capped, with `value_len`), and bounds; `marks` number the clickable
  ones with centre points in input space (`raw=true`). A failed read is
  `ok: false` with its reason (`not_running`, `accessibility_untrusted`,
  `no_window`, a busy application); it never answers with a process list.
  Measured on 27.10.2026: the old AppleScript `entire contents` walk took
  18–31 s on a 37-node window; this reads it in 1.6 s cold and 50 ms warm.
- Linux: no element reader in this skill (`ok: false`, `reason: unsupported`,
  with the window rows); OSWorld remotes return the guest's accessibility tree
  (opt-in in the bridge); SSH Macs answer `unsupported`.
- Who is in front is read fresh on every call (macOS `lsappinfo`, X11
  `xdotool getactivewindow`), never from a cached AppKit value.

Backend matrix (honest limitations):

- macOS: `screencapture` + `cliclick` + `osascript`; `sips` for downscaling;
  `lsappinfo` for the frontmost application; the native helper for windows,
  activation (AX `AXFrontmost`, then `NSRunningApplication`, then System
  Events), the element walk, grants and displays. `scroll` is **unsupported**
  (cliclick has no scroll-wheel command — page via `key` with
  `page-down`/`page-up`); `middle` click and non-left `mouse_down`/`mouse_up`
  are unsupported via cliclick. `hold_key` holds PURE-MODIFIER combos only
  (`cmd`, `cmd+shift`, ...): cliclick `kd:`/`ku:` accept only modifiers and
  `kp:` cannot hold — non-modifier holds report an honest error. In `key`
  combos the base key is tapped while modifiers are held (`cmd+s` = hold cmd,
  tap s). `type_text` with `via=paste` writes the clipboard (`pbcopy`) and
  pastes with `cmd+v` (the clipboard is replaced and the receipt says so); use
  it for long or non-ASCII text, which `cliclick t:` types slowly.
- Linux X11: `xdotool` (input and the active window), `gnome-screenshot`/`scrot`
  (capture), `wmctrl` (window rows and `app=` activation through
  `wmctrl -i -a`, verified with `xdotool getactivewindow`; without `xdotool` the
  switch is requested but reported as unverified), ImageMagick (downscaling).
  Function keys and case-sensitive keysyms are supported (`f5`→`F5`,
  `XF86AudioPlay` as-is).
- Linux Wayland: `ydotool` (pointer + typing; requires a running `ydotoold`)
  or `wtype` (typing only), `grim` or `gnome-screenshot` (capture). `xdotool`
  does NOT work on Wayland. `key`, `hold_key`, `app=` targeting and
  `window_list` are **unsupported** on Wayland (the compositor exposes no
  window list or activation to other programs; with `wmctrl` present only
  XWayland windows are listed, and the result says so); the refusal names the
  session so the owner can bring the application forward.
- Remote OSWorld HTTP: `screenshot` uses `GET <target>/screenshot`; input and
  `remote_exec` use `POST <target>/execute`, matching OSWorld's in-VM
  `pyautogui` channel. The runner writes an `osworld_http` connection before
  each task. `app=` activates in the guest (`wmctrl -a`, else `xdotool
  windowactivate`) and every input's guest snippet reads the active window
  before and after, so receipts name the guest window. Honesty guarantees on
  this backend: `scroll` clicks are 1:1 wheel detents (not multiplied);
  non-ASCII, angle brackets, multi-line or long `type_text` is pasted via the
  in-VM clipboard (`pyperclip` + `ctrl+v`, ASCII `typewrite` fallback) so
  Unicode is not silently dropped; every action's `ok` reflects the guest
  process `returncode`/`status` (HTTP 200 alone is not success); `key`
  `backspace` presses BackSpace and `fwd-delete` presses forward Delete;
  screenshots are capped at 20 MB and PNG-validated before use.
- Remote SSH macOS: `screenshot` uses remote `screencapture` + `scp`; input uses
  a remote input helper path, wrapped so the same ssh call reads the frontmost
  application before and after; `app=` takes a plain application name and
  activates it with AppleScript. `window_list` lists applications and the
  frontmost one (no window titles over SSH). The remote Mac must have Screen
  Recording and Accessibility grants configured by the owner. This skill does
  not install or store private SSH keys.

Connection registry:

- Connections live in `data/state/skills/unix_computer_use/connections.json`.
- Default active connection is `local`.
- `add_connection` can add `osworld_http` or `ssh_macos` connection metadata, but
  it never accepts private key material.
- For SSH, configure `~/.ssh/config`, `IdentityFile`, and/or `ssh-agent`
  yourself. If auth fails, `test_connection` tells you what to fix.
- Agents should add/test/activate connections only when the user or a benchmark
  runner provides an explicit target. They should not scan for arbitrary remote
  machines.
- A disabled active connection FAILS CLOSED: action tools return an error rather
  than silently falling back to the local desktop (an action aimed at a remote
  target must never land on the host).

Permissions note: the remote backends require the `net` permission. `net` needs
no owner grant (skill grants only gate `inject_chat`/`subscribe_event`), and the
launcher names this skill among the bundled seeds that start enabled anyway; the
network is used for configured connections, which the owner, a runner or the
agent can add via `add_connection`. Agents should use only explicitly provided
targets as described above. An owner who turns the skill off keeps it off across
updates. The reads declared in the manifest (`window_list`, `ax_tree`,
`capabilities`, `cursor_position`, `list_connections`) skip the per-call safety
check and are available to observe-only consciousness (owner decision
27.10.2026); `screenshot` and every input stay checked.

Layout: `plugin.py` (registration, coordinate contract, local input),
`place.py` (targets, activation, receipts, window rows, element reads),
`remote.py` (OSWorld and SSH transports), `macos_native.py` (the helper
process). The skill intentionally does not hide OS permission requirements, and
missing backends produce explicit errors with the capability report instead of
guessing. The agent should prefer semantic application APIs when available and
should ask the human before destructive or sensitive UI actions.
