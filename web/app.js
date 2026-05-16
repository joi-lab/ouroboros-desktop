/**
 * Ouroboros Web UI — Main orchestrator.
 *
 * Self-editable: this file lives in REPO_DIR and can be modified by the agent.
 * Vanilla JS, no build step. Uses ES modules for page decomposition.
 *
 * Each page is a module in web/modules/ that exports an init function.
 * This file wires them together with shared state and navigation.
 */

import { createWS } from './modules/ws.js';
import { loadVersion, initMatrixRain } from './modules/utils.js';
import { initChat } from './modules/chat.js';
import { initFiles } from './modules/files.js';

import { initLogs } from './modules/logs.js';
import { initEvolution } from './modules/evolution.js';
import { initSettings } from './modules/settings.js';
import { initCosts } from './modules/costs.js';
import { initSkills } from './modules/skills.js';
import { initWidgets } from './modules/widgets.js';
import { initUpdates } from './modules/updates.js';
import { initDashboard } from './modules/dashboard.js';
import { hydrateNavIcons } from './modules/page_icons.js';

import { initOnboardingOverlay } from './modules/onboarding_overlay.js';
import { initPwa } from './modules/pwa.js';

// ---------------------------------------------------------------------------
// Shared State
// ---------------------------------------------------------------------------
const state = {
    messages: [],
    logs: [],
    dashboard: {},
    activeFilters: { tools: true, llm: true, errors: true, tasks: true, system: true, consciousness: true },
    unreadCount: 0,
    activePage: 'chat',
    settingsActiveSubtab: 'providers',
    dashboardActiveSubtab: 'logs',
    beforePageLeave: null,
};

// ---------------------------------------------------------------------------
// WebSocket (created but not yet connected — deferred until after init)
// ---------------------------------------------------------------------------
const ws = createWS();
const beforePageLeaveHandlers = [];
let settingsControls = null;
let dashboardControls = null;

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------
async function showPage(name) {
    if (state.activePage === name) return;
    for (const handler of beforePageLeaveHandlers) {
        const canLeave = await handler({ from: state.activePage, to: name });
        if (canLeave === false) return;
    }
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(`page-${name}`)?.classList.add('active');
    document.querySelector(`.nav-btn[data-page="${name}"]`)?.classList.add('active');
    state.activePage = name;
    window.dispatchEvent(new CustomEvent('ouro:page-shown', { detail: { page: name } }));
    if (name === 'chat') {
        state.unreadCount = 0;
        updateUnreadBadge();
    }
}

async function openSettingsTab(tabName) {
    await showPage('settings');
    if (settingsControls && typeof settingsControls.activateTab === 'function') {
        settingsControls.activateTab(tabName);
    }
}

async function openDashboardTab(tabName) {
    await showPage('dashboard');
    if (dashboardControls && typeof dashboardControls.activateTab === 'function') {
        dashboardControls.activateTab(tabName);
    }
}

function updateUnreadBadge() {
    const btn = document.querySelector('.nav-btn[data-page="chat"]');
    let badge = btn?.querySelector('.unread-badge');
    if (state.unreadCount > 0 && state.activePage !== 'chat') {
        if (!badge) {
            badge = document.createElement('span');
            badge.className = 'unread-badge';
            btn.appendChild(badge);
        }
        badge.textContent = state.unreadCount > 99 ? '99+' : state.unreadCount;
    } else if (badge) {
        badge.remove();
    }
}

document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        showPage(btn.dataset.page);
    });
});
hydrateNavIcons();

// ---------------------------------------------------------------------------
// Initialize All Pages (registers WS listeners before connection opens)
// ---------------------------------------------------------------------------
const ctx = {
    ws,
    state,
    updateUnreadBadge,
    showPage,
    openSettingsTab,
    openDashboardTab,
    setBeforePageLeave: (handler) => {
        if (typeof handler !== 'function') return () => {};
        beforePageLeaveHandlers.push(handler);
        return () => {
            const idx = beforePageLeaveHandlers.indexOf(handler);
            if (idx >= 0) beforePageLeaveHandlers.splice(idx, 1);
        };
    },
};

initChat(ctx);
initFiles(ctx);
settingsControls = initSettings(ctx);
dashboardControls = initDashboard(ctx);
initLogs({ ...ctx, mount: document.getElementById('dashboard-panel-logs'), embedded: true, hostPage: 'dashboard', hostSubtab: 'logs' });
initEvolution({ ...ctx, mount: document.getElementById('dashboard-panel-evolution'), embedded: true, hostPage: 'dashboard', hostSubtab: 'evolution', chartOnly: true });
initUpdates({ ...ctx, mount: document.getElementById('dashboard-panel-updates'), hostPage: 'dashboard', hostSubtab: 'updates' });
initCosts({ ...ctx, mount: document.getElementById('dashboard-panel-costs'), embedded: true, hostPage: 'dashboard', hostSubtab: 'costs' });
initSkills(ctx);
initWidgets(ctx);

initOnboardingOverlay();

// ---------------------------------------------------------------------------
// Startup — connect WS only after all modules have registered their listeners
// ---------------------------------------------------------------------------
initMatrixRain();
loadVersion();
initPwa();

// Visual viewport height — keeps layout above soft keyboard on iOS/Android.
// Updates a <style> tag (not element.style) to set --vvh without inline styles.
// Also toggles ``keyboard-open`` so CSS can drop the mobile nav reservation
// while the soft keyboard is visible.
(function () {
    const vvhStyle = document.createElement('style');
    vvhStyle.id = 'runtime-vvh';
    document.head.appendChild(vvhStyle);

    let wasKeyboardOpen = false;
    let keyboardTouchStartY = 0;
    let frozenBaseline = 0;

    function findScrollableKeyboardNode(target) {
        let el = target;
        while (el && el !== document.body) {
            if (
                el.id === 'chat-messages'
                || el.id === 'chat-input'
                || el.classList?.contains('chat-live-timeline')
            ) return el;
            el = el.parentElement;
        }
        return null;
    }

    function lockTouchStart(e) {
        if (e.touches && e.touches.length) keyboardTouchStartY = e.touches[0].clientY;
    }

    // Keep internal scrollable chat surfaces usable, but stop their top/bottom
    // overscroll from chaining into document/visualViewport movement while the
    // keyboard is open.
    function lockBoundaryTouch(e) {
        const touch = e.touches && e.touches.length ? e.touches[0] : null;
        const scrollable = findScrollableKeyboardNode(e.target);
        if (scrollable && touch) {
            const dy = touch.clientY - keyboardTouchStartY;
            const atTop = scrollable.scrollTop <= 0;
            const atBottom = Math.ceil(scrollable.scrollTop + scrollable.clientHeight) >= scrollable.scrollHeight;
            if ((!atTop && dy > 0) || (!atBottom && dy < 0)) return;
        }
        e.preventDefault();
    }

    function captureFrozenBaseline() {
        if (window.innerWidth > 640 || wasKeyboardOpen) return;
        const candidates = [
            document.documentElement.clientHeight,
            window.innerHeight,
            window.screen.availHeight || 0,
            window.screen.height || 0,
        ];
        const best = Math.max(...candidates);
        if (best > frozenBaseline) frozenBaseline = best;
    }

    captureFrozenBaseline();

    const updateVvh = () => {
        const viewport = window.visualViewport;
        const h = viewport ? viewport.height : window.innerHeight;

        if (window.innerWidth <= 640) {
            const safeHeight = Math.max(320, Math.ceil(h || window.innerHeight || 0));
            vvhStyle.textContent = ':root{--vvh:' + safeHeight + 'px}';
            if (!wasKeyboardOpen) captureFrozenBaseline();
            const stableHeight = frozenBaseline || document.documentElement.clientHeight;
            const keyboardVisible = viewport
                ? (stableHeight - h) > Math.max(120, stableHeight * 0.25)
                : false;

            if (keyboardVisible && !wasKeyboardOpen) {
                window.scrollTo(0, 0);
                document.addEventListener('touchstart', lockTouchStart, { passive: true });
                document.addEventListener('touchmove', lockBoundaryTouch, { passive: false });
            }
            if (!keyboardVisible && wasKeyboardOpen) {
                document.removeEventListener('touchstart', lockTouchStart);
                document.removeEventListener('touchmove', lockBoundaryTouch);
            }
            document.documentElement.classList.toggle('keyboard-open', keyboardVisible);
            document.body.classList.toggle('keyboard-open', keyboardVisible);
            wasKeyboardOpen = keyboardVisible;
        } else {
            if (wasKeyboardOpen) {
                document.removeEventListener('touchstart', lockTouchStart);
                document.removeEventListener('touchmove', lockBoundaryTouch);
            }
            document.documentElement.classList.remove('keyboard-open');
            document.body.classList.remove('keyboard-open');
            wasKeyboardOpen = false;
            vvhStyle.textContent = ':root{--vvh:100dvh}';
        }
    };
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', updateVvh);
        window.visualViewport.addEventListener('scroll', updateVvh);
    }
    window.addEventListener('resize', updateVvh);
    window.addEventListener('orientationchange', () => {
        frozenBaseline = 0;
        captureFrozenBaseline();
        updateVvh();
    });
    updateVvh();
}());

ws.connect();
