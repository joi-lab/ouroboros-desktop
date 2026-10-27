/**
 * A composer capsule's panel (docs/DESIGN.md "Composer owner controls"). The capsule in the row only shows its value;
 * what it chooses lives in its panel. On a desk a float panel stands above the capsule, its leading edge on the
 * capsule's and inside the composer's column; on a phone (under 720 px) a bottom sheet rises over a light scrim. The
 * row never moves: the open capsule only takes its hover fill (`data-open`). One panel is open at a time on the page;
 * an outside press, Escape, the same capsule again or, on a phone, a swipe down closes it. Focus goes into the panel
 * (onto the chosen option when the keyboard opened it) and back to the capsule when Escape, the capsule or a swipe
 * closes it.
 *
 * The panel is a frame: the capsule's ring and name, then `body`, where the capsule puts its strip, its meaning line
 * (`meaningLines`: every value's line in one cell, so a choice never changes the panel's height) and its rule.
 */
import { leaveAsPicture, motionMs, reducedMotion } from './motion.js';

const PHONE = '(max-width: 719.98px)';
const GAP_PX = 8;            // between the capsule and the float panel above it
const SWIPE_SHARE = 1 / 3;   // a sheet let go past a third of its height goes,
const FLICK_PX_MS = 0.6;     // and so does one flung down
// --ease-sheet run backwards: a float panel that leaves eases in.
const EASE_LEAVING = 'cubic-bezier(1, 0, 0.68, 0.28)';
let current = null;          // the one open panel on the page
let sequence = 0;

/** Every value's line in one cell, only the chosen one shown: the cell is as tall as the tallest line. */
export function meaningLines(doc, lines) {
    const cell = doc.createElement('div');
    cell.className = 'capsule-panel-lines';
    const nodes = Object.fromEntries(Object.entries(lines).map(([value, text]) => {
        const line = doc.createElement('p');
        line.className = 'capsule-panel-line';
        line.dataset.value = value;
        line.textContent = text;
        cell.append(line);
        return [value, line];
    }));
    return {
        cell,
        nodes,
        show(value) {
            for (const [key, line] of Object.entries(nodes)) {
                const on = String(key === value);
                if (line.dataset.on !== on) line.dataset.on = on;
            }
        },
    };
}

/**
 * The glass thumb under the chosen option (`seg`, one of the strip's `segs`): kept from the strip's rounded ends by the
 * inset it keeps from its top and bottom, so it stays concentric with them (DESIGN: a mark keeps from a rounded end the
 * clearance it keeps from the straight edges). Not laid out (an unopened panel), it stays where it is.
 */
export function placeThumb(thumb, seg, segs) {
    if (!(Number(seg?.offsetWidth) > 0)) return;
    const inset = Number(thumb.offsetTop) || 0;
    const ends = segs.flatMap((node) => [node.offsetLeft, node.offsetLeft + node.offsetWidth]);
    const left = Math.max(seg.offsetLeft, Math.min(...ends) + inset);
    const right = Math.min(seg.offsetLeft + seg.offsetWidth, Math.max(...ends) - inset);
    thumb.style.left = `${left}px`;
    thumb.style.width = `${right - left}px`;
}

/** The quiet line under the meaning: how a change applies. */
export function ruleLine(doc, text) {
    const rule = doc.createElement('p');
    rule.className = 'capsule-panel-rule';
    rule.textContent = text;
    return rule;
}

/**
 * `el` is the capsule, `head` its button; `focusTarget()` the option a keyboard open lands on; `repaint(open)` paints
 * the capsule's strip again (open, the panel is in the document and its marks are measured there).
 */
export function createCapsulePanel({
    el, head, kind, name, focusTarget = () => null, repaint = () => {},
    win = globalThis.window, doc = globalThis.document,
}) {
    const id = `capsule-panel-${(sequence += 1)}`;
    const node = (tag, className) => {
        const made = doc.createElement(tag);
        made.className = className;
        return made;
    };
    const panel = node('div', 'capsule-panel');
    panel.id = id;
    panel.dataset.kind = kind;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', name);
    panel.tabIndex = -1;
    const grab = node('div', 'capsule-panel-grab');
    grab.setAttribute('aria-hidden', 'true');
    const top = node('div', 'capsule-panel-head');
    const glyph = node('span', 'capsule-panel-glyph');
    glyph.setAttribute('aria-hidden', 'true');
    const title = node('span', 'capsule-panel-title');
    title.textContent = name;
    top.append(glyph, title);
    const body = node('div', 'capsule-panel-body');
    panel.append(grab, top, body);
    head.setAttribute('aria-haspopup', 'dialog');
    head.setAttribute('aria-controls', id);
    head.setAttribute('aria-expanded', 'false');
    el.dataset.open = 'false';

    let open = false;
    let destroyed = false;
    let scrim = null;
    let drag = 0;
    let swipe = null;  // the swipe a finger draws now: ends on its own release, or with any close
    const api = { panel, top, body, isOpen: () => open };
    const phone = () => { try { return Boolean(win?.matchMedia?.(PHONE)?.matches); } catch { return false; } };
    const ease = () => { try { return win.getComputedStyle(doc.documentElement).getPropertyValue('--ease-sheet').trim() || 'ease-out'; } catch { return 'ease-out'; } };
    const animated = () => !reducedMotion(win) && typeof panel.animate === 'function';

    /** The capsule's ring, as it stands now. */
    function syncGlyph() {
        const svg = head.querySelector('.chat-capsule-glyph svg');
        if (svg) glyph.replaceChildren(svg.cloneNode(true));
    }

    // Desk: above the capsule, its leading edge on the capsule's, kept inside the composer's column. A sheet stands where
    // the stylesheet puts it: the same panel may have been a float before a resize, so none of a float's geometry stays.
    function place() {
        if (!open) return;
        if (panel.dataset.place !== 'float') {
            Object.assign(panel.style, { left: '', bottom: '', transformOrigin: '' });
            panel.style.setProperty('--panel-column', '');
            return;
        }
        const at = el.getBoundingClientRect();
        const column = (el.closest?.('.chat-input-wrap') || doc.body).getBoundingClientRect();
        panel.style.setProperty('--panel-column', `${Math.round(column.width)}px`);
        const width = panel.offsetWidth;
        const left = Math.max(column.left, Math.min(at.left, column.right - width));
        panel.style.left = `${Math.round(left)}px`;
        panel.style.bottom = `${Math.round((win?.innerHeight || 0) - at.top + GAP_PX)}px`;
        panel.style.transformOrigin = `${Math.round(at.left + at.width / 2 - left)}px 100%`;
    }

    // A layout change (opening, a resize) lands the strip's marks where they stand, once the panel has its width; only a
    // choice moves them.
    function settle(update) {
        panel.dataset.settling = 'true';
        update();
        void panel.offsetWidth;
        delete panel.dataset.settling;
    }

    function openPanel({ viaKeyboard = false } = {}) {
        if (open || destroyed) return;
        if (current && current !== api) current.close({ instant: true });
        current = api;
        open = true;
        const sheet = phone();
        panel.dataset.place = sheet ? 'sheet' : 'float';
        if (sheet) panel.setAttribute('aria-modal', 'true');  // Back gives it Escape first (layers.js)
        el.dataset.open = 'true';
        head.setAttribute('aria-expanded', 'true');
        syncGlyph();
        if (sheet) {
            scrim = node('div', 'capsule-panel-scrim');
            scrim.setAttribute('aria-hidden', 'true');
            scrim.addEventListener('click', () => close());  // on the click, not the press: nothing under it is pressed
            doc.body.append(scrim);
        }
        doc.body.append(panel);
        settle(() => { place(); repaint(true); });
        if (animated()) {
            const easing = ease();
            const frames = sheet ? [{ transform: 'translateY(100%)' }, { transform: 'none' }]
                : [{ opacity: 0, transform: 'scale(0.96)' }, { opacity: 1, transform: 'none' }];
            const duration = motionMs(sheet ? '--motion-open' : '--motion-fade', win);
            panel.animate(frames, { duration, easing });
            scrim?.animate([{ opacity: 0 }, { opacity: 1 }], { duration, easing });
        }
        ((viaKeyboard && focusTarget()) || panel).focus({ preventScroll: true });
    }

    function close({ focus = false, instant = false } = {}) {
        if (!open) return;
        open = false;
        swipe?.();  // closed mid-swipe (Escape, Back, the scrim): the finger moves this panel no more
        if (current === api) current = null;
        el.dataset.open = 'false';
        head.setAttribute('aria-expanded', 'false');
        panel.removeAttribute('aria-modal');  // its leaving picture is no dialog Back could reach
        repaint(false);
        if (focus) head.focus({ preventScroll: true });
        // The panel goes at once; its picture plays the leaving (motion.js), from where a swipe let it go.
        if (!instant) {
            const sheet = panel.dataset.place === 'sheet';
            const duration = motionMs(sheet ? '--motion-close' : '--motion-fade', win);
            leaveAsPicture(panel, sheet ? [{ transform: `translateY(${drag}px)` }, { transform: 'translateY(100%)' }]
                : [{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(0.96)' }],
            { duration, easing: sheet ? ease() : EASE_LEAVING, win });
            if (scrim) leaveAsPicture(scrim, [{ opacity: scrim.style.opacity || 1 }, { opacity: 0 }], { duration, easing: ease(), win });
        }
        panel.remove();
        scrim?.remove();
        scrim = null;
        panel.style.transform = '';
        drag = 0;
    }

    // A sheet follows a finger down from its grabber or head; let go past a third (or flung), it goes.
    function onSwipeStart(event) {
        if (!open || panel.dataset.place !== 'sheet' || event.button) return;
        swipe?.();  // one finger draws the sheet at a time
        const y0 = event.clientY;
        const height = panel.offsetHeight || 1;
        let lastY = y0;
        let lastT = event.timeStamp;
        let speed = 0;
        try { panel.setPointerCapture(event.pointerId); } catch { /* a synthetic event */ }
        const move = (ev) => {
            if (ev.pointerId !== event.pointerId) return;
            drag = Math.max(0, ev.clientY - y0);
            panel.style.transform = `translateY(${drag}px)`;
            if (scrim) scrim.style.opacity = String(1 - Math.min(1, drag / height));
            speed = (ev.clientY - lastY) / Math.max(1, ev.timeStamp - lastT);
            lastY = ev.clientY;
            lastT = ev.timeStamp;
        };
        const stop = () => {
            panel.removeEventListener('pointermove', move);
            panel.removeEventListener('pointerup', end);
            panel.removeEventListener('pointercancel', end);
            swipe = null;
        };
        const end = (ev) => {
            if (ev.pointerId !== event.pointerId) return;
            stop();
            if (open && (drag > height * SWIPE_SHARE || speed > FLICK_PX_MS)) { close({ focus: true }); return; }
            const from = drag;
            drag = 0;
            panel.style.transform = '';
            if (scrim) scrim.style.opacity = '';
            if (from > 0 && animated()) {
                panel.animate([{ transform: `translateY(${from}px)` }, { transform: 'none' }],
                    { duration: motionMs('--motion-close', win), easing: ease() });
            }
        };
        panel.addEventListener('pointermove', move);
        panel.addEventListener('pointerup', end);
        panel.addEventListener('pointercancel', end);
        swipe = stop;
    }

    // A click with detail 0 is Enter or Space on the focused capsule: a keyboard open lands on the chosen option.
    const onHeadClick = (event) => {
        if (open) close({ focus: true });
        else openPanel({ viaKeyboard: event.detail === 0 });
    };
    // An outside press closes (a sheet's scrim closes on its click); a press on a capsule of a composer row is that
    // capsule's to answer (it closes its own panel or switches to its own at once).
    const onDocPointerDown = (event) => {
        if (!open) return;
        const target = event.target;
        if (panel.contains(target) || head.contains(target) || target === scrim) return;
        if (!scrim && target?.closest?.('.chat-toolbar-row .chat-capsule-head')) return;
        close();
    };
    const onDocKeyDown = (event) => {
        if (event.key !== 'Escape' || !open || event.defaultPrevented) return;
        event.preventDefault();  // the panel took this Escape: the Project room under it stays open
        close({ focus: true });
    };
    const onFocusOut = (event) => {
        const next = event.relatedTarget;
        if (open && next && !panel.contains(next) && next !== head) close();
    };
    const onResize = () => {
        if (!open) return;
        if ((panel.dataset.place === 'sheet') !== phone()) close({ instant: true });
        else settle(() => { place(); repaint(true); });
    };

    head.addEventListener('click', onHeadClick);
    doc.addEventListener('pointerdown', onDocPointerDown, true);
    doc.addEventListener('keydown', onDocKeyDown);
    panel.addEventListener('focusout', onFocusOut);
    grab.addEventListener('pointerdown', onSwipeStart);
    top.addEventListener('pointerdown', onSwipeStart);
    win?.addEventListener?.('resize', onResize);

    return Object.assign(api, {
        open: openPanel,
        close,
        place,
        syncGlyph,
        destroy() {
            if (destroyed) return;
            close({ instant: true });
            destroyed = true;
            head.removeEventListener('click', onHeadClick);
            doc.removeEventListener('pointerdown', onDocPointerDown, true);
            doc.removeEventListener('keydown', onDocKeyDown);
            panel.removeEventListener('focusout', onFocusOut);
            grab.removeEventListener('pointerdown', onSwipeStart);
            top.removeEventListener('pointerdown', onSwipeStart);
            win?.removeEventListener?.('resize', onResize);
        },
    });
}
