/**
 * The Subagents capsule (docs/DESIGN.md "Composer owner controls"): the composer's third capsule beside Context and
 * Effort, saving its room's subagents mode, Off · Auto · Swarm, through the owner endpoint. Each room keeps its own
 * value (a new Project copies Main's once); a change applies to new steps in this chat while running helpers finish.
 * Its mark is a small network (owner 27.10, G): Ouroboros joined by threads to three helpers; none lit for Off, one
 * helper and its thread for Auto, every helper and the threads between them for Swarm. Closed, it says the value: Swarm in the switched-on capsule's soft crimson, Off muted,
 * Auto plain. When Settings › Agents has subagents off, or they are not set up yet, it says Off with a lock: only Off
 * can be chosen, the reason is said and Open Settings goes there; the room's own value is kept for when they are on.
 *
 * The opened body is one component, `createSubagentsChoice()`: the choice (the strip of three values under the glass
 * thumb Context's modes use) and what it means (the chosen value's one line, the rule line, or the reason and Open
 * Settings). The capsule hosts the strip on the composer's line and the note under that line, so the row grows upward
 * and the field stays.
 */
import { haptic } from './haptics.js';

export const SUBAGENT_MODES = Object.freeze(['off', 'auto', 'swarm']);
const WORDS = Object.freeze({ off: 'Off', auto: 'Auto', swarm: 'Swarm' });
export const SUBAGENT_MEANINGS = Object.freeze({
    off: 'Ouroboros works alone in this chat: no subagents, no Claude Code or Codex sessions. Reviews still run.',
    auto: 'Ouroboros decides when helpers are worth it.',
    swarm: "Don't economise: independent views of several models, critics, one synthesis. Costs more.",
});
export const SUBAGENT_RULE = 'Applies to new steps in this chat; running helpers finish.';
// Why only Off can be chosen (`subagents_availability` in the room's state); `available` or an unknown answer locks nothing.
export const SUBAGENT_LOCKS = Object.freeze({
    disabled: 'Subagents are turned off in Settings › Agents',
    unsaved: 'Subagents are not set up yet',
});
const modeOf = (mode) => (SUBAGENT_MODES.includes(mode) ? mode : 'auto');

/* The mark: Ouroboros (the large node), each helper with the thread to it, and the threads between helpers, which
   only Swarm lights (there helpers talk to each other). Every part paints with currentColor; style.css colours it. */
const helperMark = (n, x, y, r) => `<g class="chat-subagents-helper" data-helper="${n}">`
    + `<path d="M66 110 L${x} ${y}" fill="none" stroke="currentColor" stroke-width="11" stroke-linecap="round"></path>`
    + `<circle cx="${x}" cy="${y}" r="${r}" fill="currentColor"></circle></g>`;
const helperLink = (d) => `<path class="chat-subagents-link" d="${d}" fill="none" stroke="currentColor" stroke-width="8" stroke-linecap="round"></path>`;
export const SUBAGENTS_MARK = helperLink('M144 120 L122 40') + helperLink('M122 40 L42 46')
    + helperMark(1, 144, 120, 19) + helperMark(2, 122, 40, 17) + helperMark(3, 42, 46, 15)
    + '<circle class="chat-subagents-core" cx="66" cy="110" r="28" fill="currentColor"></circle>';
const LOCK = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" '
    + 'stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="10" rx="2.5"></rect>'
    + '<path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>';

/** What the capsule says for a room's stored mode and the catalog's availability. */
export function subagentsView(mode, availability = 'available') {
    const lock = SUBAGENT_LOCKS[availability] || '';
    const shown = lock ? 'off' : modeOf(mode);
    return {
        mode: shown, stored: modeOf(mode), locked: Boolean(lock), reason: lock, word: WORDS[shown],
        meaning: SUBAGENT_MEANINGS[shown],
        said: lock ? `Subagents: Off. ${lock}.` : `Subagents: ${WORDS[shown]}. ${SUBAGENT_MEANINGS[shown]} ${SUBAGENT_RULE}`,
    };
}

/**
 * The opened body. `onChoose(mode)` saves a choice; `onOpenSettings()` goes to Settings › Agents. Returns its two
 * parts and `paint(view)` (a subagentsView), `chosen()` (the chosen value's node) and `step(event)` for the keyboard.
 */
export function createSubagentsChoice({ doc = globalThis.document, onChoose = () => {}, onOpenSettings = () => {} } = {}) {
    const strip = doc.createElement('div');
    strip.className = 'chat-context-strip chat-subagents-strip';
    strip.setAttribute('role', 'radiogroup');
    strip.setAttribute('aria-label', 'Subagents');
    strip.dataset.scroll = 'none';
    strip.innerHTML = '<span class="chat-context-thumb" aria-hidden="true"></span>' + SUBAGENT_MODES.map((mode) =>
        `<button class="chat-seg" type="button" role="radio" tabindex="-1" aria-checked="false" data-mode="${mode}">`
        + `<span class="chat-seg-text">${WORDS[mode]}</span></button>`).join('');
    const segs = Array.from(strip.querySelectorAll('.chat-seg'));

    const note = doc.createElement('div');
    note.className = 'chat-subagents-note';
    note.setAttribute('data-capsule-note', '');  // the line under the strip, never a peer the strip is fitted beside
    note.hidden = true;
    note.innerHTML = '<p class="chat-subagents-meaning"></p><p class="chat-subagents-rule"></p>'
        + '<div class="chat-subagents-locked"><p class="chat-subagents-reason"></p>'
        + '<button type="button" class="btn btn-default btn-sm chat-subagents-settings ui-hit-area">Open Settings</button></div>';
    const meaning = note.querySelector('.chat-subagents-meaning');
    const rule = note.querySelector('.chat-subagents-rule');
    const locked = note.querySelector('.chat-subagents-locked');
    const reason = note.querySelector('.chat-subagents-reason');
    const settings = note.querySelector('.chat-subagents-settings');
    rule.textContent = SUBAGENT_RULE;

    let view = subagentsView('auto');
    let open = false;
    const enabled = (seg) => seg.dataset.disabled !== 'true';
    const chosen = () => segs.find((seg) => seg.dataset.mode === view.mode);

    function paint(next = view, { isOpen = open } = {}) {
        view = next;
        open = isOpen;
        for (const seg of segs) {
            const on = seg.dataset.mode === view.mode;
            const off = view.locked && seg.dataset.mode !== 'off';
            seg.setAttribute('aria-checked', String(on));
            seg.dataset.disabled = String(off);
            if (off) seg.setAttribute('aria-disabled', 'true'); else seg.removeAttribute('aria-disabled');
            seg.tabIndex = open && on ? 0 : -1;  // one tab stop; arrows move within
        }
        // The thumb lies under the chosen value: its measured place, written as the strip's own two properties.
        const seg = chosen();
        if (Number(seg?.offsetWidth) > 0) {
            strip.style.setProperty('--thumb-left', `${seg.offsetLeft}px`);
            strip.style.setProperty('--thumb-width', `${seg.offsetWidth}px`);
        }
        // Words are written only when they change: the row's text observer refits on every write
        // (composer_owner_controls.js), and a refit paints again.
        if (meaning.textContent !== view.meaning) meaning.textContent = view.meaning;
        if (reason.textContent !== view.reason) reason.textContent = view.reason;
        meaning.hidden = view.locked;
        rule.hidden = view.locked;
        locked.hidden = !view.locked;
        note.hidden = !open;
    }

    const onClick = (event) => {
        const seg = event.target?.closest?.('.chat-seg');
        if (!seg || !enabled(seg) || seg.dataset.mode === view.mode) return;
        haptic('tick');  // another value, chosen by a press (haptics.js)
        onChoose(seg.dataset.mode);
    };
    const onSettings = () => onOpenSettings();
    strip.addEventListener('click', onClick);
    settings.addEventListener('click', onSettings);

    /** Arrows, Home and End move among the values that can be chosen and choose the one they land on. */
    function step(event) {
        const live = segs.filter(enabled);
        const at = live.indexOf(event.target?.closest?.('.chat-seg'));
        if (at < 0) return null;
        const delta = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
        const to = delta ? Math.min(live.length - 1, Math.max(0, at + delta))
            : event.key === 'Home' ? 0 : event.key === 'End' ? live.length - 1 : -1;
        if (to < 0) return null;
        event.preventDefault?.();
        live[to].focus({ preventScroll: true });
        if (live[to].dataset.mode !== view.mode) onChoose(live[to].dataset.mode);
        return live[to];
    }

    return {
        strip, note, segs, paint, chosen, step,
        destroy() {
            strip.removeEventListener('click', onClick);
            settings.removeEventListener('click', onSettings);
            note.remove();
        },
    };
}

/**
 * The capsule in its room's composer row. `capsule(options)` is the family's open/fit machine
 * (composer_owner_controls.js), `save({ mode })` the room's owner write (it answers the room's cognition),
 * `openSettings()` Settings › Agents. Returns `{ el, capsule, syncState(values, availability), hasPendingSave,
 * pendingSave, destroy }`.
 */
export function createSubagentsControl({
    el, row, capsule: createCapsule, save, openSettings = () => {}, showToast, refreshState = () => {}, onLayout,
    onSettled, win, doc,
}) {
    el.classList.add('chat-capsule');
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'Subagents');
    el.dataset.known = 'false';
    el.innerHTML = '<button type="button" class="chat-capsule-head ui-hit-area chat-subagents-head" aria-expanded="false">'
        + `<span class="chat-capsule-glyph" aria-hidden="true"><svg viewBox="0 0 184 184">${SUBAGENTS_MARK}</svg></span>`
        + '<span class="chat-capsule-name">Subagents</span><span class="chat-capsule-value" data-i18n-skip></span>'
        + `<span class="chat-subagents-lock" aria-hidden="true">${LOCK}</span></button>`
        + '<div class="chat-capsule-body"></div>';
    const head = el.querySelector('.chat-subagents-head');
    const value = el.querySelector('.chat-capsule-value');
    let stored = 'auto';
    let availability = 'available';
    let saving = null, lastOk = true, destroyed = false;
    let control = null;

    const choice = createSubagentsChoice({
        doc,
        onChoose: (mode) => choose(mode),
        onOpenSettings: () => { control?.setOpen(false); openSettings(); },
    });
    el.querySelector('.chat-capsule-body').append(choice.strip);
    row.append(choice.note);

    function render() {
        const view = subagentsView(stored, availability);
        el.dataset.mode = view.mode;
        el.dataset.locked = String(view.locked);
        if (value.textContent !== view.word) value.textContent = view.word;
        head.title = view.said;
        head.setAttribute('aria-label', view.said);
        choice.paint(view, { isOpen: Boolean(control?.isOpen()) });
    }

    function choose(mode) {
        if (saving || destroyed || !SUBAGENT_MODES.includes(mode) || mode === stored) return undefined;
        if (availability in SUBAGENT_LOCKS && mode !== 'off') return undefined;
        el.dataset.disabled = 'true';
        saving = (async () => {
            try {
                const reply = await save({ mode });
                if (destroyed) return false;
                stored = modeOf(reply?.subagents_mode);
                el.dataset.known = 'true';
                lastOk = true;
            } catch (error) {
                if (destroyed) return false;
                lastOk = false;
                showToast(error?.message || 'Could not change subagents.', 'error');
            } finally {
                saving = null;
                if (!destroyed) {
                    el.dataset.disabled = 'false';
                    render();
                    refreshState(true);
                }
            }
            return lastOk;
        })();
        return saving;
    }

    const revealChosen = () => {
        const seg = choice.chosen();
        if (seg) control.reveal(Number(seg.offsetLeft), Number(seg.offsetLeft) + Number(seg.offsetWidth));
    };
    control = createCapsule({
        el, row, kind: 'subagents', strip: choice.strip, segs: choice.segs, render, focusTarget: choice.chosen,
        note: choice.note, padMax: 11, onLayout, onSettled, win, doc,
        afterOpen: revealChosen,
        afterOverflow: () => revealChosen(),
    });
    const onKey = (event) => { if (choice.step(event)) revealChosen(); };
    choice.strip.addEventListener('keydown', onKey);
    render();

    return {
        el,
        capsule: control,
        /** The room's stored mode (Main's state or the Project's cognition) and the catalog's availability; a save in
         *  flight answers for itself. An answer without the mode keeps the last one. */
        syncState(values, nextAvailability) {
            if (typeof nextAvailability === 'string') availability = nextAvailability;
            if (typeof values?.subagents_mode === 'string' && !saving) {
                stored = modeOf(values.subagents_mode);
                el.dataset.known = 'true';
            }
            render();
        },
        hasPendingSave: () => Boolean(saving),
        pendingSave: () => saving || Promise.resolve(lastOk),
        destroy() {
            destroyed = true;
            control.destroy();
            choice.strip.removeEventListener('keydown', onKey);
            choice.destroy();
        },
    };
}
