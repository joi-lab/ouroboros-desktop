// What a Swarm message says and what its root did (docs/DESIGN.md "Composer owner controls"). The quiet
// `Swarm` in an owner message's time line is the room's Subagents value when the host accepted that message: a request,
// never proof that helpers ran. One line on a Swarm root's card says only what the root's receipt records (SYN2 A.8:
// helpers started, models, the money known and what is not final yet, subscription sessions), never minds, waves,
// families, peaks, percentages or one sum the facts do not make; an unknown amount is a word, never $0.
import { SUBAGENTS_RING } from './composer_subagents.js';
import { formatUsd2 } from './utils.js';

const counted = (count, one) => `${count} ${one}${count === 1 ? '' : 's'}`;

function ownBubble(feed, row) {
    const cmid = String(row?.client_message_id || '');
    return cmid ? [...(feed?.querySelectorAll('.chat-bubble.user[data-client-message-id]') || [])]
        .find((node) => node.dataset.clientMessageId === cmid) || null : null;
}

/** Puts `Swarm ·` before the time of an owner message the host accepted under Swarm. The bubble is the row's own, or
 *  (the owner's own echo) the one in `feed` that carries the row's client message id. */
export function markSwarmLabel(feed, row, bubble = ownBubble(feed, row)) {
    const time = row?.role === 'user' && row.subagents_mode === 'swarm' ? bubble?.querySelector('.msg-time') : null;
    if (!time || time.querySelector('.msg-swarm')) return false;
    const doc = time.ownerDocument;
    const label = doc.createElement('span');
    label.className = 'msg-swarm';
    label.title = 'Sent while Subagents was on Swarm';
    label.innerHTML = `<svg viewBox="0 0 184 184" aria-hidden="true">${SUBAGENTS_RING}</svg>`;
    const word = doc.createElement('span');
    word.textContent = 'Swarm';
    label.append(word);
    const dot = doc.createElement('span');
    dot.className = 'msg-swarm-sep';
    dot.setAttribute('aria-hidden', 'true');
    dot.textContent = '·';
    time.prepend(label, dot);
    return true;
}

/** The one line a Swarm root's card says from its recorded facts, or '' while nothing is recorded. */
export function swarmReceiptText(detail) {
    const metadata = detail?.metadata;
    if (metadata?.subagents_mode !== 'swarm' || detail.parent_task_id || metadata.parent_task_id) return '';
    const efficiency = detail.swarm_efficiency && typeof detail.swarm_efficiency === 'object' ? detail.swarm_efficiency : null;
    const cost = detail.swarm_receipt?.cost && typeof detail.swarm_receipt.cost === 'object' ? detail.swarm_receipt.cost : null;
    if (!efficiency && !cost) return '';
    const helpers = Math.max(0, Math.trunc(Number(efficiency?.subagent_count) || 0));
    if (efficiency && !helpers) return 'Swarm requested · no helpers started';
    const attempts = (Array.isArray(cost?.attempts) ? cost.attempts : []).filter((attempt) => attempt && typeof attempt === 'object');
    const models = new Set(attempts.map((attempt) => String(attempt.model || '')).filter(Boolean)).size;
    // A session is its task: a resumed session is one more attempt of the same session.
    const sessions = new Set(attempts.filter((attempt) => attempt.kind === 'subscription_session')
        .map((attempt) => String(attempt.task_id || attempt.attempt_id || ''))).size;
    const known = Number(cost?.known_final_usd) > 0 ? `${formatUsd2(cost.known_final_usd)} known` : '';
    const open = Math.trunc(Number(cost?.unknown_attempts) || 0);
    return ['Swarm', helpers && `${counted(helpers, 'helper')} started`, models && counted(models, 'model'),
        [known, open > 0 ? `${open} not final` : ''].filter(Boolean).join(' + '),
        sessions && counted(sessions, 'subscription session')].filter(Boolean).join(' · ');
}

/** The receipt line, first in a root card's foot, once the root's detail carries its facts; an event or a row without
 *  them changes nothing. */
export function syncSwarmReceipt(record, detail) {
    const text = record?.root && record.footEl && !record.isSubagent ? swarmReceiptText(detail) : '';
    if (!text || record.swarmReceiptEl?.textContent === text) return false;
    if (!record.swarmReceiptEl) {
        record.swarmReceiptEl = record.root.ownerDocument.createElement('div');
        record.swarmReceiptEl.className = 'chat-live-swarm-receipt';
        record.swarmReceiptEl.setAttribute('data-i18n-skip', '');
        record.footEl.prepend(record.swarmReceiptEl);
    }
    record.swarmReceiptEl.textContent = text;
    return true;
}
