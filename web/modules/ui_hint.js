/**
 * The one hint (docs/DESIGN.md "Hints"): what a control says about itself. It stands above its control and over the
 * page on the menus' floating material (ui.css), so showing it, changing its words or hiding it moves nothing. It
 * lives inside its control: the control's own press, hover and focus handling covers it, a reader hears it as the
 * control's description, and Tab reaches its one action right after the control.
 *
 * `say({ word, text, rule, action })` writes only the words that changed; a row that watches its words for a refit
 * leaves the hint's alone (composer_owner_controls.js). `onAction()` runs the action.
 */
let made = 0;
const EDGE_PX = 8;

export function createHint(host, { doc = host.ownerDocument, onAction = () => {} } = {}) {
    const el = doc.createElement('div');
    el.className = 'ui-hint';
    el.id = `ui-hint-${++made}`;
    el.hidden = true;
    el.innerHTML = '<p class="ui-hint-line"><span class="ui-hint-word"></span><span class="ui-hint-text"></span></p>'
        + '<p class="ui-hint-rule"></p><button type="button" class="ui-hint-action ui-hit-area"></button>';
    const parts = {
        word: el.querySelector('.ui-hint-word'), text: el.querySelector('.ui-hint-text'),
        rule: el.querySelector('.ui-hint-rule'), action: el.querySelector('.ui-hint-action'),
    };
    const write = (node, words = '') => {
        if (node.textContent !== words) node.textContent = words;
        node.hidden = !words;
    };
    const onClick = () => onAction();
    parts.action.addEventListener('click', onClick);
    host.append(el);

    // It starts where its control starts; where it would pass the window's far edge it moves back, never past the
    // near one. Right to left, the start is the control's right edge.
    function place() {
        const width = Number(doc.documentElement?.clientWidth);
        if (!Number.isFinite(width) || width <= 0) return;
        el.style.setProperty('--ui-hint-shift', '0px');
        const box = el.getBoundingClientRect();
        let dx = 0;
        if (box.right > width - EDGE_PX) dx = width - EDGE_PX - box.right;
        if (box.left + dx < EDGE_PX) dx = EDGE_PX - box.left;
        const rtl = doc.defaultView?.getComputedStyle?.(el)?.direction === 'rtl';
        if (dx) el.style.setProperty('--ui-hint-shift', `${Math.round(rtl ? -dx : dx)}px`);
    }

    return {
        el,
        say({ word = '', text = '', rule = '', action = '' } = {}) {
            write(parts.word, word);
            write(parts.text, text);
            write(parts.rule, rule);
            write(parts.action, action);
            if (!el.hidden) place();
        },
        show() {
            if (!el.hidden) return;
            el.hidden = false;
            place();
        },
        hide() { el.hidden = true; },
        destroy() {
            parts.action.removeEventListener('click', onClick);
            el.remove();
        },
    };
}
