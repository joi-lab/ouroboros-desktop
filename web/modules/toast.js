import { normalizeTone } from './ui_helpers.js';

function getStack() {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        stack.className = 'toast-stack';
        stack.setAttribute('aria-live', 'polite');
        stack.setAttribute('aria-relevant', 'additions');
        document.body.appendChild(stack);
    }
    return stack;
}

export function showToast(message, tone = 'info', { ttl = 6000 } = {}) {
    const stack = getStack();
    const toast = document.createElement('div');
    const cleanTone = normalizeTone(tone || 'info', 'info');
    toast.className = `toast toast-${cleanTone}`;
    toast.setAttribute('role', cleanTone === 'danger' ? 'alert' : 'status');
    toast.textContent = message || '';
    stack.appendChild(toast);
    const dismiss = () => {
        toast.classList.add('is-hiding');
        setTimeout(() => toast.remove(), 180);
    };
    if (ttl > 0) setTimeout(dismiss, ttl);
    toast.addEventListener('click', dismiss);
    return toast;
}
