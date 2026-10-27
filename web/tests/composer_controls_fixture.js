// The composer's owner controls mounted on stub elements (composer_owner_controls.test.js, composer_subagents.test.js
// and composer_owner_haptics.test.js): a document whose listeners can be fired and whose body takes an open panel, a
// desk or a phone window, one effort capsule or the whole composer row as chat.js builds it, and the presses that
// drive them. A capsule's strip lives in its panel (capsule_panel.js), found through the control.
import { ElementStub } from './chat_dom_fixture.js';
import { createComposerOwnerControls, createEffortRangeControl } from '../modules/composer_owner_controls.js';

/* A document whose listeners can be fired; an open panel joins its body. */
export function stubDocument() {
    const listeners = new Map();
    const doc = {
        byId: new Map(), activeElement: null,
        createElement: (tag) => new ElementStub(tag, doc),
        addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]); },
        removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter((f) => f !== fn)); },
        fire(type, event) { for (const fn of listeners.get(type) || []) fn(event); },
        count: (type) => (listeners.get(type) || []).length,
    };
    doc.documentElement = new ElementStub('html', doc);
    doc.body = new ElementStub('body', doc);
    doc.body.isConnected = true;
    return doc;
}

/* A desk window by default; `phone` answers the panel's phone query (under 720 px). Its listeners can be fired. */
export function stubWindow({ phone = false, ...extra } = {}) {
    const listeners = new Map();
    return {
        innerHeight: 800,
        matchMedia: (query) => ({ matches: phone && query === '(max-width: 719.98px)' }),
        addEventListener: (type, fn) => listeners.set(type, [...(listeners.get(type) || []), fn]),
        removeEventListener: (type, fn) => listeners.set(type, (listeners.get(type) || []).filter((f) => f !== fn)),
        fire: (type) => (listeners.get(type) || []).forEach((fn) => fn({})),
        count: (type) => (listeners.get(type) || []).length,
        ...extra,
    };
}
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const escapeEvent = () => ({ key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } });

export function mount({ save, win, phone = false } = {}) {
    const doc = stubDocument();
    const row = doc.createElement('div');
    row.className = 'chat-toolbar-row';
    row.getBoundingClientRect = () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });  // not laid out: no fit
    const toasts = [];
    const refreshes = [];
    const saves = [];
    const control = createEffortRangeControl({
        row, doc, win: win || stubWindow({ phone }),
        saveEffortRange: save || (async (triple) => { saves.push(triple); return { ok: true, effort_range: triple }; }),
        showToast: (message, tone) => toasts.push([message, tone]),
        refreshState: (force) => refreshes.push(force),
    });
    const el = control.el;
    const panel = control.panel.panel;
    const strip = panel.querySelector('[data-effort-strip]');
    const segs = strip.querySelectorAll('.chat-effort-seg');
    // The strip has layout: seven 40px segments, so a boundary is x / 40.
    segs.forEach((seg, i) => { seg.offsetLeft = i * 40; seg.offsetWidth = 40; });
    const head = el.querySelector('.chat-effort-head');
    const handles = { min: strip.querySelector('.chat-effort-min'), rec: strip.querySelector('.chat-effort-rec'), max: strip.querySelector('.chat-effort-max') };
    const fire = (node, type, event) => { for (const fn of node.listeners.get(type) || []) fn(event); };
    const last = (node, type) => (node.listeners.get(type) || []).at(-1);
    const pointer = (clientX, { handle = null, pointerId = 1, clientY = 10 } = {}) => ({
        button: 0, clientX, clientY, pointerId, pointerType: 'mouse', preventDefault() {},
        target: { closest: (selector) => (selector === '[data-handle]' ? handle : null) },
    });
    return {
        control, el, row, doc, head, panel, strip, handles, segs, toasts, refreshes, saves,
        headClick: (detail = 1) => fire(head, 'click', { detail }),
        key: (which, key) => fire(handles[which], 'keydown', { key, currentTarget: handles[which], preventDefault() {} }),
        down: (x, options) => fire(strip, 'pointerdown', pointer(x, options)),
        move: (x, pointerId = 1) => last(strip, 'pointermove')?.(pointer(x, { pointerId })),
        up: (x, pointerId = 1) => last(strip, 'pointerup')?.(pointer(x, { pointerId })),
        cancel: (pointerId = 1) => last(strip, 'pointercancel')?.(pointer(0, { pointerId })),
        outside: () => doc.fire('pointerdown', { target: new ElementStub('div', doc) }),
        inside: () => doc.fire('pointerdown', { target: strip }),
        escape: () => { const event = escapeEvent(); doc.fire('keydown', event); return event; },
        settle: () => control.pendingSave(),
    };
}

/* The composer row as chat.js builds it: the Subagents anchor, then the context anchor; Effort mounts after Context.
   `subagents` mounts the Subagents capsule on its anchor too (its writer answers `subagentAnswer`). */
export function mountPair({ reply, win, phone = false, withBox = false, projectId = '', subagents = false } = {}) {
    const doc = stubDocument();
    const box = doc.createElement('section');
    const row = doc.createElement('div');
    box.appendChild(row);
    row.className = 'chat-toolbar-row';
    row.getBoundingClientRect = () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 });  // not laid out: no fit
    const subagentsEl = doc.createElement('div');
    subagentsEl.className = 'chat-subagents-mode';
    row.appendChild(subagentsEl);
    const contextEl = doc.createElement('div');
    contextEl.className = 'chat-context-mode';
    contextEl.dataset.contextMode = 'max';
    row.appendChild(contextEl);
    const posts = [];
    const toasts = [];
    const refreshes = [];
    const answer = { value: reply || { ok: true } };
    const effortAnswer = { value: null };
    const subagentAnswer = { value: null };
    const settingsOpened = [];
    const subagentsWriter = subagents ? {
        saveSubagentsMode: async (body) => {
            posts.push(['/api/owner/subagents-mode', body]);
            const reply = await subagentAnswer.value;
            return reply || { ok: true, project_id: projectId, subagents_mode: body.mode, cognition: { subagents_mode: body.mode } };
        },
        openSettings: () => settingsOpened.push('agents'),
    } : {};
    const window = win || stubWindow({ phone });
    const controls = createComposerOwnerControls({
        ...subagentsWriter,
        projectId, row, box: withBox ? box : null, doc, win: window,
        byId: (suffix) => (suffix === 'context-mode' ? contextEl : suffix === 'subagents-mode' ? subagentsEl : null),
        apiFetch: async (url, init) => {
            const body = JSON.parse(init.body); posts.push([url, body]);
            const response = await answer.value;
            return response.json ? response : { ...response, json: async () => ({ project_id: projectId, context_mode: body.mode }) };
        },
        saveEffortRange: async (body) => { posts.push(['/api/owner/effort-range', body]); return effortAnswer.value || { project_id: projectId, effort_range: body }; },
        showToast: (message, tone) => toasts.push([message, tone]),
        refreshState: (force) => refreshes.push(force),
    });
    const fire = (node, type, event) => Promise.all((node.listeners.get(type) || []).map((fn) => fn(event)));
    const effortEl = controls.effort.el;
    const panels = { context: controls.context.panel.panel, effort: controls.effort.panel.panel, subagents: controls.subagents?.panel.panel };
    const contextStrip = panels.context.querySelector('.chat-context-strip');
    const subStrip = panels.subagents?.querySelector('.chat-subagents-strip');
    const seg = (mode) => contextStrip.querySelectorAll('.chat-seg').find((node) => node.dataset.mode === mode);
    const subSeg = (mode) => subStrip?.querySelectorAll('.chat-seg').find((node) => node.dataset.mode === mode);
    const onSeg = (find, mode) => ({ closest: (selector) => (selector === '.chat-seg' ? find(mode) : null) });
    const heads = {
        context: contextEl.querySelector('.chat-context-head'), effort: effortEl.querySelector('.chat-effort-head'),
        subagents: subagentsEl.querySelector('.chat-subagents-head'),
    };
    // A press on another capsule's head is found as one, as `closest` finds it in a browser.
    for (const head of Object.values(heads)) {
        if (head) head.closest = (selector) => (selector.includes('chat-capsule-head') ? head : null);
    }
    return {
        controls, row, box, doc, win: window, contextEl, effortEl, posts, toasts, refreshes, answer, effortAnswer, seg,
        panels, contextStrip, subStrip,
        subagentsEl, subagentAnswer, settingsOpened, subSeg, subHead: heads.subagents,
        subNote: panels.subagents?.querySelector('.chat-subagents-note'),
        chooseSub: (mode) => fire(subStrip, 'click', { target: onSeg(subSeg, mode) }),
        subKey: (mode, key) => {
            const event = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, target: onSeg(subSeg, mode) };
            fire(subStrip, 'keydown', event);
            return event;
        },
        contextHead: heads.context, effortHead: heads.effort,
        press: (head, detail = 1) => fire(head, 'click', { detail }),
        choose: (mode) => fire(contextStrip, 'click', { target: onSeg(seg, mode) }),
        key: (mode, key) => fire(contextStrip, 'keydown', { key, preventDefault() {}, target: onSeg(seg, mode) }),
        docDown: (target) => doc.fire('pointerdown', { target }),
        escape: () => { const event = escapeEvent(); doc.fire('keydown', event); return event; },
        open: () => doc.body.children.filter((node) => node.classList.contains('capsule-panel')),
    };
}
