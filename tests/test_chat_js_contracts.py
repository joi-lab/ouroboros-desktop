"""Contract tests for web/modules/chat.js.

History: PR #23 originally proposed a richer recall/plan-prefix pipeline
(``stripPlanPrefix`` helper, ``_recallText`` capture of live inbound
messages, ``Set``-based dedup, explicit ``inputHistory.length > 50`` cap).
That surface never landed in chat.js — the current implementation uses a
simpler model: inline ``PLAN_PREFIX + text`` in ``sendMessage``,
last-element dedup in ``rememberInput``, and ``slice(-50)`` at both
``loadInputHistory`` and ``saveInputHistory``. History replay and live
inbound bubbles show the raw ``msg.text`` / ``msg.content`` (the plan
preamble is intentionally visible so the user can see what was actually
sent on the wire).

This file now has two complementary layers aligned with that reality:
1. Structural (source-text) — verify the actual patterns in chat.js
   (``slice(-50)``, last-element dedup, inline ``PLAN_PREFIX + text``
   etc.). Breaks on deletion/rename.
2. Executable (logic port) — preserve the Python ports of the
   ``stripPlanPrefix`` and ``extractRecallEntries`` helpers so that if
   they ever get implemented in JS, the Python-port tests are the
   authoritative spec for their behaviour.
"""
import pathlib
import re

CHAT_JS = pathlib.Path(__file__).parent.parent / "web" / "modules" / "chat.js"

_PLAN_PREFIX = (
    "Please do multi-model planning (plan_task tool) and web-search before "
    "answering or starting this task:\n\n"
)


def _src() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


# ── Portable Python ports of the JS pure helpers ─────────────────────────────

def _strip_plan_prefix(text: str) -> str:
    """Python port of chat.js::stripPlanPrefix."""
    return text[len(_PLAN_PREFIX):].lstrip() if text.startswith(_PLAN_PREFIX) else text


def _extract_recall_entries(messages: list, existing=None) -> list:
    """Python port of chat.js::extractRecallEntries."""
    seen = set(existing or [])
    out = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = _strip_plan_prefix((msg.get("text") or "").strip())
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


# ── visibilitychange fix ──────────────────────────────────────────────────────

def test_chat_js_has_single_visibilitychange_listener():
    """PR #23 unified: exactly one visibilitychange listener covering both layout sync and scroll restore."""
    src = _src()
    count = src.count("document.addEventListener('visibilitychange'")
    assert count == 1, (
        f"Expected exactly 1 visibilitychange listener (unified handler), found {count}"
    )


def test_chat_js_visibilitychange_calls_sync_live_card_layout():
    """The visibilitychange handler calls syncLiveCardLayout to fix stale layout."""
    src = _src()
    # The handler block should reference syncLiveCardLayout and requestAnimationFrame
    assert "syncLiveCardLayout" in src
    assert "requestAnimationFrame" in src


def test_chat_js_visibilitychange_restores_scroll():
    """The visibilitychange handler restores scroll position on tab return."""
    src = _src()
    assert "scrollTop = messagesDiv.scrollHeight" in src or \
           "messagesDiv.scrollTop = messagesDiv.scrollHeight" in src


# ── ArrowUp recall: seeding sources ──────────────────────────────────────────

# inputHistory has two writers:
#   1. rememberInput() — called from sendMessage on local sends.
#   2. syncHistory() seeding block — seeds ONCE per page lifetime from
#      server-side user messages (Telegram, other WebUI sessions), gated
#      by the one-shot inputHistorySeededFromServer flag. Subsequent WS
#      reconnects deliberately do NOT re-seed (avoids inputHistoryIndex
#      reset mid-scrub); new server-side messages that arrive over a
#      persistent tab surface in recall only after the next page reload.
# Live inbound ws.on('chat') does NOT push to inputHistory (avoids mid-scrub
# UX disruption and the mid-scrub index reset issue).


def test_chat_js_sync_history_seeds_input_history_via_dedicated_flag():
    """syncHistory gates recall seeding on the dedicated `inputHistorySeededFromServer`
    flag, NOT on `!historyLoaded`.  This ensures seeding still fires on the first
    successful server sync even when historyLoaded was already set true by the
    sessionStorage-fallback bootstrap path (e.g. first /api/chat/history fetch failed)."""
    src = _src()
    sync_start = src.index("async function syncHistory(")
    sync_end = src.index("\n    (async () => {", sync_start)
    sync_body = src[sync_start:sync_end]
    assert "!inputHistorySeededFromServer" in sync_body, (
        "syncHistory must gate seeding on !inputHistorySeededFromServer, not !historyLoaded"
    )
    assert "inputHistorySeededFromServer = true" in sync_body, (
        "syncHistory must set inputHistorySeededFromServer = true after seeding"
    )
    assert "inputHistory.push" in sync_body, (
        "syncHistory must push to inputHistory during seeding"
    )
    assert "saveInputHistory(inputHistory)" in sync_body, (
        "syncHistory seeding must persist updated inputHistory"
    )


def test_chat_js_sync_history_seeding_gate_is_dedicated_flag():
    """The seeding block must be wrapped by `if (!inputHistorySeededFromServer)`.
    Other uses of `!historyLoaded || fromReconnect` in syncHistory (e.g. retiredTaskIds)
    are fine — only the seeding gate matters."""
    src = _src()
    sync_start = src.index("async function syncHistory(")
    sync_end = src.index("\n    (async () => {", sync_start)
    sync_body = src[sync_start:sync_end]
    assert "if (!inputHistorySeededFromServer)" in sync_body, (
        "syncHistory seeding block must be gated by `if (!inputHistorySeededFromServer)`, "
        "not by `!historyLoaded || fromReconnect`"
    )


def test_chat_js_sync_history_strips_plan_prefix_in_seeding():
    """syncHistory strips PLAN_PREFIX from server-side user messages before
    adding them to inputHistory, so plan-mode preambles don't pollute recall."""
    src = _src()
    sync_start = src.index("async function syncHistory(")
    sync_end = src.index("\n    (async () => {", sync_start)
    sync_body = src[sync_start:sync_end]
    assert "PLAN_PREFIX" in sync_body, (
        "syncHistory seeding must reference PLAN_PREFIX to strip planning preambles"
    )
    assert "startsWith(PLAN_PREFIX)" in sync_body, (
        "syncHistory seeding must check startsWith(PLAN_PREFIX) before stripping"
    )


def test_chat_js_input_history_seeded_flag_declared():
    """inputHistorySeededFromServer flag must be declared in initChat scope
    alongside historyLoaded so it survives across syncHistory calls."""
    src = _src()
    assert "inputHistorySeededFromServer = false" in src, (
        "inputHistorySeededFromServer must be declared and initialised to false in initChat"
    )


def test_chat_js_plan_prefix_defined_at_module_scope():
    """PLAN_PREFIX is defined at module scope (before initChat) so both
    sendMessage and syncHistory can reference it without redefinition."""
    src = _src()
    plan_prefix_idx = src.index("const PLAN_PREFIX =")
    init_chat_idx = src.index("export function initChat(")
    assert plan_prefix_idx < init_chat_idx, (
        "PLAN_PREFIX const must be defined at module scope, before initChat"
    )


def test_chat_js_dedupes_input_history_via_last_element_check():
    """inputHistory dedups by checking the last element only (not a Set).

    The simpler last-element model catches the common ArrowUp-then-send
    round-trip without allocating a Set on every recall entry.
    """
    src = _src()
    assert "inputHistory[inputHistory.length - 1] !== text" in src, (
        "rememberInput must dedup by checking the last element of inputHistory"
    )


def test_chat_js_caps_input_history_via_slice_50():
    """inputHistory is bounded at 50 entries.

    ``slice(-50)`` must appear at least 3 times:
      1. ``loadInputHistory`` — caps on load from sessionStorage
      2. ``saveInputHistory`` — caps on persist
      3. ``syncHistory`` seeding block — caps merged server+local recall
    """
    src = _src()
    assert src.count("slice(-50)") >= 3, (
        "inputHistory must be bounded via slice(-50) at load, save, AND syncHistory seeding"
    )


def test_chat_js_sync_history_seeding_uses_chronological_merge():
    """syncHistory seeding builds a chronological merge: server messages (older)
    prepended before local session messages (newer), then deduped from the end
    so most-recent entries are kept, then capped at 50 via slice(-50).

    This prevents the naive-append bug where server messages appear *after*
    local messages in recall, inverting chronological order for ArrowUp."""
    src = _src()
    sync_start = src.index("async function syncHistory(")
    sync_end = src.index("\n    (async () => {", sync_start)
    sync_body = src[sync_start:sync_end]

    # Must build a combined array from server + local texts
    assert "serverTexts" in sync_body, (
        "syncHistory seeding must collect server messages into serverTexts array"
    )
    assert "...serverTexts, ...inputHistory" in sync_body or \
           "serverTexts, ...inputHistory" in sync_body, (
        "syncHistory seeding must combine serverTexts (older) with inputHistory (newer)"
    )
    # Must deduplicate from end (newest wins) — iterate from i = combined.length - 1
    assert "combined.length - 1" in sync_body, (
        "syncHistory seeding must deduplicate from the end to preserve most-recent entries"
    )
    # Must cap at 50 via slice(-50)
    assert "slice(-50)" in sync_body, (
        "syncHistory seeding must cap the merged recall at 50 entries via slice(-50)"
    )
    # Must replace inputHistory in-place
    assert "inputHistory.length = 0" in sync_body, (
        "syncHistory seeding must replace inputHistory in-place (inputHistory.length = 0)"
    )


# ── Live inbound handling ─────────────────────────────────────────────────────

def test_chat_js_does_not_append_live_inbound_to_input_history():
    """Live inbound user messages (Telegram, other sessions) are rendered as
    bubbles but do NOT pollute the local user's ArrowUp recall — only
    ``sendMessage`` adds entries to inputHistory via ``rememberInput``."""
    src = _src()
    # ws.on('chat') user branch renders bubble but does not push to inputHistory.
    ws_chat_block = src[src.index("ws.on('chat'"):src.index("ws.on('chat'") + 2000]
    assert "inputHistory.push" not in ws_chat_block, (
        "ws.on('chat') handler must not push to inputHistory (only local sends do)"
    )
    # rememberInput is the sole writer; it is called from sendMessage.
    assert "rememberInput(text)" in src


def test_chat_js_saves_input_history_on_remember():
    """``rememberInput`` must persist to sessionStorage after the push so the
    in-memory array and sessionStorage stay in sync. Scoped to the
    ``rememberInput`` function body so a globally-placed
    ``saveInputHistory(...)`` call elsewhere cannot accidentally satisfy this
    contract (reviewer finding from v4.40.1 review pass 3)."""
    src = _src()
    fn_start = src.index("function rememberInput(text) {")
    # End of the function: the next line that starts with "    }" at the
    # exact same indent as the function definition.
    fn_end = src.index("\n    }\n", fn_start) + len("\n    }")
    body = src[fn_start:fn_end]

    assert "inputHistory.push(text)" in body, (
        "rememberInput must push the raw user text into inputHistory"
    )
    assert "saveInputHistory(inputHistory)" in body, (
        "rememberInput must persist the updated inputHistory via saveInputHistory"
    )

    # Ordering: the save must follow the push so the stored value matches the
    # in-memory array after the mutation, not before.
    push_idx = body.index("inputHistory.push(text)")
    save_idx = body.index("saveInputHistory(inputHistory)")
    assert push_idx < save_idx, (
        "saveInputHistory must be called AFTER inputHistory.push in rememberInput"
    )


# ── PLAN_PREFIX application ───────────────────────────────────────────────────

_PLAN_PREFIX = 'Please do multi-model planning (plan_task tool) and web-search before answering or starting this task:\n\n'


def test_chat_js_applies_plan_prefix_inline_in_sendmessage():
    """``sendMessage`` applies PLAN_PREFIX inline via ``PLAN_PREFIX + text`` when
    ``planMode`` is on and the text is not a slash command. There is no
    dedicated ``stripPlanPrefix`` helper — the prefix is applied at send
    time and the wire text is stored/rendered as-is everywhere."""
    src = _src()
    assert "PLAN_PREFIX + text" in src, (
        "sendMessage must apply PLAN_PREFIX inline via 'PLAN_PREFIX + text'"
    )
    assert "planMode && !text.startsWith('/')" in src, (
        "PLAN_PREFIX application must be guarded by planMode + slash-command bypass"
    )


def test_chat_js_sync_history_renders_raw_msg_text():
    """History replay does not strip PLAN_PREFIX — user bubbles in recall
    show the exact wire text (plan preamble visible). This is intentional
    so the user can audit what was actually sent."""
    src = _src()
    # Find the syncHistory region and verify the bubble is rendered from raw msg.text.
    sync_start = src.index("async function syncHistory(")
    # Bound to the next top-level function; use a generous lookahead.
    sync_end = src.index("\n    async function ", sync_start + 1) if "\n    async function " in src[sync_start + 1:] else sync_start + 20000
    sync_body = src[sync_start:sync_end]
    assert "stripPlanPrefix" not in sync_body, (
        "syncHistory must not use stripPlanPrefix — raw msg.text is rendered"
    )
    assert "addMessage(msg.text" in sync_body, (
        "syncHistory must call addMessage(msg.text, ...) directly for history replay"
    )


def test_chat_js_live_inbound_renders_raw_msg_content():
    """``ws.on('chat')`` live inbound user path calls ``addMessage(msg.content,
    'user', ...)`` directly without stripping PLAN_PREFIX — same rationale
    as history replay."""
    src = _src()
    ws_start = src.index("ws.on('chat'")
    ws_end = src.index("ws.on('", ws_start + 1) if "ws.on('" in src[ws_start + 1:] else ws_start + 3000
    ws_body = src[ws_start:ws_end]
    assert "stripPlanPrefix" not in ws_body, (
        "ws.on('chat') must not use stripPlanPrefix — raw msg.content is rendered"
    )
    assert "addMessage(msg.content" in ws_body, (
        "ws.on('chat') must render live inbound user bubbles via addMessage(msg.content, ...)"
    )


def test_plan_prefix_string_defined_exactly_once():
    """PLAN_PREFIX canonical string appears exactly once (const definition in
    ``sendMessage``). There is no second occurrence because no
    ``stripPlanPrefix`` helper exists in the current implementation."""
    src = _src()
    canonical = 'Please do multi-model planning (plan_task tool) and web-search before answering or starting this task:'
    occurrences = src.count(canonical)
    assert occurrences == 1, (
        f"Expected PLAN_PREFIX canonical string exactly 1 time (const only), found {occurrences}"
    )


# ── Regression guards: recall/send pipeline ───────────────────────────────────

def test_chat_js_remember_input_runs_before_wire_prefix():
    """``rememberInput`` must capture the raw user text *before* ``PLAN_PREFIX``
    is prepended to ``wireText`` — otherwise ArrowUp recall would resurface
    the plan preamble instead of the original text."""
    src = _src()
    remember_idx = src.index("rememberInput(text)")
    wire_idx = src.index("const wireText = (planMode")
    assert remember_idx < wire_idx, (
        "rememberInput(text) must run before the PLAN_PREFIX wireText assignment"
    )


# ── Regression guards ─────────────────────────────────────────────────────────

def test_chat_js_input_history_index_reset_after_seeding():
    """inputHistoryIndex is reset to length after recall seeding."""
    src = _src()
    assert "inputHistoryIndex = inputHistory.length" in src


# ── Executable behavior tests (Python port of JS pure helpers) ────────────────

def test_strip_plan_prefix_removes_prefix():
    """stripPlanPrefix strips the plan preamble and trims leading whitespace."""
    result = _strip_plan_prefix(_PLAN_PREFIX + "What is 2+2?")
    assert result == "What is 2+2?"


def test_strip_plan_prefix_leaves_normal_text_unchanged():
    """stripPlanPrefix does not modify messages that don't start with PLAN_PREFIX."""
    msg = "Hello, what can you do?"
    assert _strip_plan_prefix(msg) == msg


def test_strip_plan_prefix_handles_empty_string():
    """stripPlanPrefix handles empty input gracefully."""
    assert _strip_plan_prefix("") == ""


def test_extract_recall_entries_filters_non_user_roles():
    """extractRecallEntries only includes user-role messages."""
    messages = [
        {"role": "assistant", "text": "Hello"},
        {"role": "user", "text": "Hi there"},
        {"role": "system", "text": "System msg"},
    ]
    result = _extract_recall_entries(messages)
    assert result == ["Hi there"]


def test_extract_recall_entries_strips_plan_prefix():
    """extractRecallEntries strips PLAN_PREFIX before adding to recall."""
    messages = [{"role": "user", "text": _PLAN_PREFIX + "Search for X"}]
    result = _extract_recall_entries(messages)
    assert result == ["Search for X"]


def test_extract_recall_entries_deduplicates_against_existing():
    """extractRecallEntries skips messages already in the existing set."""
    existing = {"already there"}
    messages = [
        {"role": "user", "text": "already there"},
        {"role": "user", "text": "new message"},
    ]
    result = _extract_recall_entries(messages, existing)
    assert result == ["new message"]


def test_extract_recall_entries_deduplicates_within_batch():
    """extractRecallEntries skips duplicate messages within the same batch."""
    messages = [
        {"role": "user", "text": "hello"},
        {"role": "user", "text": "hello"},
    ]
    result = _extract_recall_entries(messages)
    assert result == ["hello"]


def test_extract_recall_entries_skips_empty_text():
    """extractRecallEntries skips messages with empty or whitespace-only text."""
    messages = [
        {"role": "user", "text": ""},
        {"role": "user", "text": "   "},
        {"role": "user", "text": "real message"},
    ]
    result = _extract_recall_entries(messages)
    assert result == ["real message"]


def test_plan_prefix_js_constant_matches_python_port():
    """The canonical PLAN_PREFIX string in chat.js must match the Python port
    so that the Python port's ``_strip_plan_prefix`` would successfully strip
    what ``sendMessage`` actually sends on the wire."""
    src = _src()
    # PLAN_PREFIX in chat.js may be written as a template literal with \n\n
    # escapes or as a regular string; accept both forms.
    assert _PLAN_PREFIX.replace("\n\n", "\\n\\n") in src or _PLAN_PREFIX in src, (
        "PLAN_PREFIX const in chat.js must match the Python port's canonical string"
    )


class TestRenderMarkdownMdTableWrap:
    """renderMarkdown wraps markdown tables in .md-table-wrap for overflow scrolling."""

    def test_table_output_contains_md_table_wrap(self):
        content = open("web/modules/utils.js").read()
        assert 'class="md-table-wrap"' in content, (
            "renderMarkdown must wrap tables in <div class=\"md-table-wrap\"> for horizontal overflow"
        )

    def test_md_table_wrap_css_rule_exists(self):
        css = open("web/style.css").read()
        assert ".md-table-wrap" in css, (
            ".md-table-wrap CSS rule must exist in web/style.css"
        )

    def test_md_table_wrap_has_overflow_x_auto(self):
        css = open("web/style.css").read()
        # Find the .md-table-wrap block and verify it has overflow-x: auto
        idx = css.find(".md-table-wrap")
        assert idx != -1, ".md-table-wrap not found in style.css"
        block = css[idx:idx+200]
        assert "overflow-x: auto" in block, (
            ".md-table-wrap must have overflow-x: auto for horizontal scrolling"
        )


class TestRenderMarkdownNoInlineStyles:
    """renderMarkdown must not use inline style= attributes — use CSS classes instead."""

    def test_no_inline_style_in_header_replacement(self):
        content = open("web/modules/utils.js").read()
        # Extract the renderMarkdown function body
        start = content.find("export function renderMarkdown")
        end = content.find("\nexport function", start + 1)
        fn_body = content[start:end] if end != -1 else content[start:]
        # Headers should use CSS classes, not inline styles
        assert 'style="font-size' not in fn_body, (
            "renderMarkdown headers must use CSS classes (md-h1/md-h2/md-h3), not inline style= attributes"
        )

    def test_no_inline_style_in_list_replacement(self):
        content = open("web/modules/utils.js").read()
        start = content.find("export function renderMarkdown")
        end = content.find("\nexport function", start + 1)
        fn_body = content[start:end] if end != -1 else content[start:]
        # List items should use CSS class, not inline style
        assert 'style="display:block;padding-left' not in fn_body, (
            "renderMarkdown list items must use CSS class md-li, not inline style= attributes"
        )

    def test_no_inline_style_in_link_replacement(self):
        content = open("web/modules/utils.js").read()
        start = content.find("export function renderMarkdown")
        end = content.find("\nexport function", start + 1)
        fn_body = content[start:end] if end != -1 else content[start:]
        # Links should use CSS class, not inline style
        assert 'style="color:var(--accent)' not in fn_body, (
            "renderMarkdown links must use CSS class md-link, not inline style= attributes"
        )

    def test_markdown_css_classes_defined_in_stylesheet(self):
        css = open("web/style.css").read()
        for cls in [".md-h1", ".md-h2", ".md-h3", ".md-li", ".md-link"]:
            assert cls in css, f"{cls} must be defined as a CSS rule in web/style.css"


class TestVisualViewportListener:
    """web/app.js must contain the visualViewport listener that drives --vvh."""

    def test_visual_viewport_listener_present(self):
        content = open("web/app.js").read()
        assert "window.visualViewport" in content, (
            "web/app.js must contain a visualViewport listener to update --vvh CSS token"
        )

    def test_vvh_property_set_in_listener(self):
        content = open("web/app.js").read()
        assert "--vvh" in content, (
            "web/app.js must set --vvh CSS custom property via visualViewport listener"
        )

    def test_vvh_css_variable_defined_in_root(self):
        css = open("web/style.css").read()
        assert "--vvh:" in css, (
            "--vvh CSS custom property must be defined in :root in web/style.css"
        )

    def test_body_uses_vvh_not_100vh(self):
        css = open("web/style.css").read()
        # body block should use var(--vvh), not 100vh directly
        body_idx = css.find("body {")
        body_block = css[body_idx:body_idx+200]
        assert "var(--vvh)" in body_block, (
            "body must use height: var(--vvh) for keyboard-safe mobile layout"
        )
        assert "100vh" not in body_block, (
            "body must not use 100vh directly — use var(--vvh) which falls back to 100dvh"
        )


class TestRenderMarkdownLinkSanitization:
    """renderMarkdown must sanitize unsafe URL schemes and add rel=noopener noreferrer."""

    def test_safe_https_link_preserved(self):
        content = open("web/modules/utils.js").read()
        # Safe https:// links should pass the allowlist
        assert '/^https?:/' in content or "^https?:" in content, (
            "renderMarkdown must allowlist https:// scheme for links"
        )

    def test_unsafe_scheme_falls_back_to_hash(self):
        content = open("web/modules/utils.js").read()
        # javascript: and other unsafe schemes must be blocked — the safe variable falls back to '#'
        # Code pattern: const safe = /^https?:|^mailto:/i.test(url) ? url : '#';
        assert ": '#'" in content or ': "#"' in content or "? url : '#'" in content or '? url : "#"' in content, (
            "renderMarkdown must fall back to '#' for unsafe URL schemes (const safe = ... ? url : '#')"
        )

    def test_rel_noopener_noreferrer_on_links(self):
        content = open("web/modules/utils.js").read()
        # All generated anchors must have rel=noopener noreferrer
        assert 'rel="noopener noreferrer"' in content or "rel='noopener noreferrer'" in content, (
            "renderMarkdown must emit rel=\"noopener noreferrer\" on generated anchors"
        )

    def test_mailto_scheme_allowed(self):
        content = open("web/modules/utils.js").read()
        # mailto: is a safe and useful scheme that should be allowed
        assert "mailto:" in content, (
            "renderMarkdown should allowlist mailto: scheme in link sanitizer"
        )


# ── Live-card memory leak guards (2026-05-04) ────────────────────────────────
#
# Three accumulation sites in chat.js made browser memory grow monotonically
# over long sessions: record.items per task, the matching DOM nodes under
# record.timelineEl, and finished-task records in the liveCardRecords Map.
# The dedupe-against-last-only mitigation in applyLiveCardState fails on
# interleaved retry storms (e.g. alternating provider_incomplete_response +
# llm_usage during a stuck-round loop), so a single wedged task can push
# hundreds of entries before recovery.
#
# These contract tests pin the caps + helpers so the guards can't be silently
# removed in a refactor.

def test_chat_js_declares_live_timeline_caps():
    """MAX_LIVE_TIMELINE_ITEMS and MAX_LIVE_CARD_RECORDS constants must exist."""
    src = _src()
    assert "MAX_LIVE_TIMELINE_ITEMS" in src, (
        "chat.js must declare MAX_LIVE_TIMELINE_ITEMS to bound record.items per task"
    )
    assert "MAX_LIVE_CARD_RECORDS" in src, (
        "chat.js must declare MAX_LIVE_CARD_RECORDS to bound liveCardRecords.size"
    )


def test_chat_js_trim_record_items_helper_exists():
    """trimRecordItems(record) must exist and shift items + DOM together."""
    src = _src()
    assert "function trimRecordItems(record)" in src, (
        "chat.js must define trimRecordItems(record) to bound record.items"
    )
    # The helper must shift the array AND remove the matching first DOM child
    # in lockstep. Without the DOM removal the timeline node count outgrows
    # the array — same leak shape, different surface.
    trim_start = src.index("function trimRecordItems(record)")
    trim_end = src.index("\n    }", trim_start)
    trim_body = src[trim_start:trim_end]
    assert "record.items.shift()" in trim_body, (
        "trimRecordItems must shift the oldest item off record.items"
    )
    assert "record.timelineEl.firstElementChild" in trim_body, (
        "trimRecordItems must remove the matching first DOM child to keep "
        "the timeline DOM bounded in lockstep with record.items"
    )
    assert "expandedLineKeys.delete" in trim_body, (
        "trimRecordItems must delete the shifted item's lineKey from "
        "record.expandedLineKeys to keep that Set bounded too"
    )


def test_chat_js_calls_trim_after_pushing_to_record_items():
    """Every record.items.push site that can grow unboundedly must be paired
    with a trimRecordItems(record) call. Today that's the append branch in
    applyLiveCardState — when dedupe-against-last fails, a new item is pushed
    and the cap must engage."""
    src = _src()
    # Exact call must appear after the push in the append branch.
    push_idx = src.index("record.items.push({")
    after_push = src[push_idx:push_idx + 800]
    assert "trimRecordItems(record)" in after_push, (
        "record.items.push must be followed by trimRecordItems(record) "
        "to bound timeline growth on retry storms"
    )


def test_chat_js_evicts_finished_records_when_map_full():
    """evictOldestFinishedRecordIfFull() must exist and be called from
    createLiveCardRecord before the new record is set, so liveCardRecords.size
    stays bounded at MAX_LIVE_CARD_RECORDS over long sessions."""
    src = _src()
    assert "function evictOldestFinishedRecordIfFull()" in src, (
        "chat.js must define evictOldestFinishedRecordIfFull() to bound "
        "liveCardRecords.size when finished records accumulate"
    )
    evict_start = src.index("function evictOldestFinishedRecordIfFull()")
    evict_end = src.index("\n    }", evict_start)
    evict_body = src[evict_start:evict_end]
    # Only finished cards may be evicted — running cards must be untouched.
    assert "rec?.finished" in evict_body or "rec.finished" in evict_body, (
        "evictOldestFinishedRecordIfFull must only evict records where "
        "rec.finished is true (running cards must be retained)"
    )
    assert "liveCardRecords.delete(" in evict_body, (
        "evictOldestFinishedRecordIfFull must call liveCardRecords.delete()"
    )
    assert ".root.remove()" in evict_body or "root.remove()" in evict_body, (
        "evictOldestFinishedRecordIfFull must remove the evicted record's "
        "DOM node so it doesn't linger detached in memory"
    )

    # Must be invoked at the only insertion site: createLiveCardRecord.
    create_start = src.index("function createLiveCardRecord(")
    create_end = src.index("\n    function getLiveCardRecord(", create_start)
    create_body = src[create_start:create_end]
    assert "evictOldestFinishedRecordIfFull()" in create_body, (
        "createLiveCardRecord must call evictOldestFinishedRecordIfFull() "
        "before liveCardRecords.set() to enforce the cap"
    )
