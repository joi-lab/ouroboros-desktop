/**
 * Progressive Web App: service worker registration and update prompts.
 */

function canRegisterServiceWorker() {
    return typeof window !== 'undefined'
        && 'serviceWorker' in navigator
        && (window.isSecureContext || location.hostname === 'localhost');
}

async function notifyUpdateReady(registration) {
    if (!registration.waiting) return;
    const shouldReload = window.confirm(
        'A new version of Ouroboros is ready. Reload now to apply it?',
    );
    if (!shouldReload) return;
    registration.waiting.postMessage({ type: 'SKIP_WAITING' });
    registration.waiting.addEventListener('statechange', (event) => {
        if (event.target.state === 'activated') {
            window.location.reload();
        }
    });
}

export function initPwa() {
    if (!canRegisterServiceWorker()) return;

    navigator.serviceWorker.register('/sw.js', { scope: '/' })
        .then((registration) => {
            if (registration.waiting) {
                notifyUpdateReady(registration);
            }
            registration.addEventListener('updatefound', () => {
                const installing = registration.installing;
                if (!installing) return;
                installing.addEventListener('statechange', () => {
                    if (installing.state === 'installed' && navigator.serviceWorker.controller) {
                        notifyUpdateReady(registration);
                    }
                });
            });
        })
        .catch((err) => {
            console.warn('PWA service worker registration failed:', err);
        });

    let refreshing = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (refreshing) return;
        refreshing = true;
        window.location.reload();
    });
}
