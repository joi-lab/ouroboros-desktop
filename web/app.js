/** Web UI orchestrator: shared state, navigation, page init, WS startup. */

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
import { apiFetch } from './modules/api_client.js';
import { escapeHtmlAttr, escapeHtmlText } from './modules/utils.js';

import { initOnboardingOverlay } from './modules/onboarding_overlay.js';
import { initPwa } from './modules/pwa.js';

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

// Connect only after modules register listeners.
const ws = createWS();
const beforePageLeaveHandlers = [];
let settingsControls = null;
let dashboardControls = null;
let navWidgetsLoaded = false;
let activeSidebarWidgetKey = '';

async function showPage(name) {
    if (state.activePage === name) return;
    for (const handler of beforePageLeaveHandlers) {
        const canLeave = await handler({ from: state.activePage, to: name });
        if (canLeave === false) return;
    }
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.mobile-profile-navbtn[data-profile-page]').forEach(b => b.classList.remove('active'));
    document.getElementById(`page-${name}`)?.classList.add('active');
    document.querySelector(`.nav-btn[data-page="${name}"]`)?.classList.add('active');
    document.querySelector(`.mobile-profile-navbtn[data-profile-page="${name}"]`)?.classList.add('active');
    const chatNavBtn = document.querySelector('.mobile-left-navbtn[data-left-page="chat"]');
    if (chatNavBtn) chatNavBtn.classList.toggle('active', name === 'chat');
    const MOBILE_PAGE_TITLES = { files: 'Файлы', dashboard: 'Дашборд', skills: 'Навыки', settings: 'Настройки' };
    const isSubpage = name in MOBILE_PAGE_TITLES;
    document.body.classList.toggle('mobile-subpage', isSubpage);
    const titleEl = document.getElementById('mobile-page-title');
    if (titleEl) titleEl.textContent = MOBILE_PAGE_TITLES[name] || '';
    if (name !== 'widgets') {
        activeSidebarWidgetKey = '';
        document.querySelectorAll('.nav-widget-item.active').forEach((item) => item.classList.remove('active'));
    }
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

function setSidebarCollapsed(collapsed) {
    document.body.classList.toggle('nav-collapsed', collapsed);
    const toggle = document.getElementById('nav-collapse-toggle');
    if (toggle) {
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        toggle.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
    }
    try {
        localStorage.setItem('ouro_nav_collapsed', collapsed ? '1' : '0');
    } catch {}
}

function initSidebarChrome() {
    const toggle = document.getElementById('nav-collapse-toggle');
    let collapsed = false;
    try {
        collapsed = localStorage.getItem('ouro_nav_collapsed') === '1';
    } catch {}
    setSidebarCollapsed(collapsed);
    toggle?.addEventListener('click', () => setSidebarCollapsed(!document.body.classList.contains('nav-collapsed')));
}

function widgetIconSrcForTitle(title = '') {
    const value = String(title).toLowerCase();
    if (value.includes('weather') || value.includes('погод')) return '/static/icons/weather.svg';
    if (value.includes('image') || value.includes('kandinsky') || value.includes('карт')) return '/static/icons/image.svg';
    if (value.includes('chart') || value.includes('graph') || value.includes('граф')) return '/static/icons/evolution.svg';
    if (value.includes('chat') || value.includes('чат')) return '/static/icons/chat.svg';
    return '/static/icons/skills.svg';
}

async function refreshSidebarWidgets() {
    const list = document.getElementById('nav-widget-list');
    if (!list) return;
    try {
        const resp = await apiFetch('/api/extensions', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        const tabs = Array.isArray(data.live?.ui_tabs) ? data.live.ui_tabs : [];
        navWidgetsLoaded = true;
        if (!tabs.length) {
            list.innerHTML = '';
            return;
        }
        list.innerHTML = tabs.map((tab) => {
            const key = String(tab.key || `${tab.skill}:${tab.tab_id}`);
            const title = String(tab.title || tab.tab_id || tab.skill || 'Widget');
            const subtitle = tab.skill && tab.skill !== title ? String(tab.skill) : '';
            const activeClass = key === activeSidebarWidgetKey ? ' active' : '';
            return `
                <button class="nav-widget-item${activeClass}" type="button" data-widget-key="${escapeHtmlAttr(key)}" title="${escapeHtmlAttr(title)}">
                    <span class="nav-widget-icon" aria-hidden="true"><img src="${escapeHtmlAttr(widgetIconSrcForTitle(title))}" alt=""></span>
                    <span class="nav-widget-copy">
                        <span class="nav-widget-title">${escapeHtmlText(title)}</span>
                        ${subtitle ? `<span class="nav-widget-subtitle">${escapeHtmlText(subtitle)}</span>` : ''}
                    </span>
                </button>
            `;
        }).join('');
    } catch (error) {
        list.innerHTML = '';
    }
}

document.getElementById('nav-widget-list')?.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-widget-key]');
    if (!button) return;
    activeSidebarWidgetKey = button.dataset.widgetKey || '';
    document.querySelectorAll('.nav-widget-item.active').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
    await showPage('widgets');
    window.dispatchEvent(new CustomEvent('ouro:widget-open', { detail: { key: button.dataset.widgetKey || '' } }));
});

initSidebarChrome();
refreshSidebarWidgets();
window.addEventListener('ouro:widgets-updated', refreshSidebarWidgets);
window.addEventListener('ouro:skill-lifecycle', (event) => {
    const action = event.detail?.action;
    if (['enable', 'disable', 'install', 'uninstall'].includes(action)) {
        refreshSidebarWidgets();
    }
});

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
initLogs({ ...ctx, mount: document.getElementById('dashboard-panel-logs') });
initEvolution({ ...ctx, mount: document.getElementById('dashboard-panel-evolution') });
initUpdates({ ...ctx, mount: document.getElementById('dashboard-panel-updates') });
initCosts({ ...ctx, mount: document.getElementById('dashboard-panel-costs') });
initSkills(ctx);
initWidgets(ctx);

initOnboardingOverlay();

initMatrixRain();
loadVersion();
initPwa();

// Mobile soft-keyboard handling: --vvh + keyboard-open without inline styles.
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

    // Stop chat overscroll from moving the document while the keyboard is open.
    function lockBoundaryTouch(e) {
        const touch = e.touches && e.touches.length ? e.touches[0] : null;
        if (touch && keyboardTouchStartY === 0) {
            // On some mobile browsers the first touchmove can fire before
            // touchstart; seed baseline and let this frame pass through.
            keyboardTouchStartY = touch.clientY;
            return;
        }
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
                keyboardTouchStartY = 0;
            }
            document.documentElement.classList.toggle('keyboard-open', keyboardVisible);
            document.body.classList.toggle('keyboard-open', keyboardVisible);
            wasKeyboardOpen = keyboardVisible;
        } else {
            if (wasKeyboardOpen) {
                document.removeEventListener('touchstart', lockTouchStart);
                document.removeEventListener('touchmove', lockBoundaryTouch);
                keyboardTouchStartY = 0;
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

// ---------------------------------------------------------------------------
// Mobile drawer system
// ---------------------------------------------------------------------------
(function initMobileDrawers() {
    const backdrop = document.getElementById('mobile-drawer-backdrop');
    const navOpenBtn = document.getElementById('mobile-nav-open');
    const leftDrawer = document.getElementById('mobile-left-drawer');
    const leftCloseBtn = document.getElementById('mobile-left-close');

    // Widgets: moved between nav-rail and mobile drawer on open/close
    const widgetListSrc = document.getElementById('nav-widget-list');
    const widgetsSection = document.getElementById('nav-widgets-section');
    const mobileWidgetsHost = document.getElementById('mobile-widgets-host');

    function openNavDrawer() {
        document.body.classList.add('mobile-nav-open');
        navOpenBtn?.setAttribute('aria-expanded', 'true');
        // Move widget list into mobile drawer (preserves all event handlers)
        if (widgetListSrc && mobileWidgetsHost && !mobileWidgetsHost.contains(widgetListSrc)) {
            mobileWidgetsHost.appendChild(widgetListSrc);
        }
    }

    function closeNavDrawer() {
        document.body.classList.remove('mobile-nav-open');
        navOpenBtn?.setAttribute('aria-expanded', 'false');
        // Move widget list back to nav-rail
        if (widgetListSrc && widgetsSection && !widgetsSection.contains(widgetListSrc)) {
            widgetsSection.appendChild(widgetListSrc);
        }
    }

    function closeAll() {
        closeNavDrawer();
    }

    navOpenBtn?.addEventListener('click', () => {
        if (document.body.classList.contains('mobile-nav-open')) closeNavDrawer();
        else openNavDrawer();
    });

    leftCloseBtn?.addEventListener('click', closeNavDrawer);
    backdrop?.addEventListener('click', closeAll);
    document.getElementById('mobile-back-btn')?.addEventListener('click', () => showPage('chat'));

    // Left drawer: pages + chat shortcut + widgets + budget.
    leftDrawer?.addEventListener('click', (e) => {
        const pageBtn = e.target.closest('[data-left-page]');
        if (pageBtn) {
            showPage(pageBtn.dataset.leftPage);
            closeNavDrawer();
            return;
        }
        const profilePageBtn = e.target.closest('[data-profile-page]');
        if (profilePageBtn) {
            showPage(profilePageBtn.dataset.profilePage);
            closeNavDrawer();
            return;
        }
        if (e.target.closest('#mobile-budget-row-btn') || e.target.closest('#mobile-budget-pill-mirror')) {
            closeNavDrawer();
            openDashboardTab('costs');
            return;
        }
        if (e.target.closest('[data-widget-key]')) {
            setTimeout(closeNavDrawer, 100);
        }
    });

    // Sync status badge from #chat-status to #mobile-status-mirror
    const statusSrc = document.getElementById('chat-status');
    const statusMirror = document.getElementById('mobile-status-mirror');
    if (statusSrc && statusMirror) {
        const syncStatus = () => {
            statusMirror.className = statusSrc.className;
            statusMirror.textContent = statusSrc.textContent;
        };
        syncStatus();
        new MutationObserver(syncStatus).observe(statusSrc, { childList: true, subtree: true, attributes: true, characterData: true });
    }

    // Sync budget text from #chat-budget-text to #mobile-budget-text-mirror
    const budgetTextSrc = document.getElementById('chat-budget-text');
    const budgetTextMirror = document.getElementById('mobile-budget-text-mirror');
    if (budgetTextSrc && budgetTextMirror) {
        const syncBudgetText = () => { budgetTextMirror.textContent = budgetTextSrc.textContent; };
        syncBudgetText();
        new MutationObserver(syncBudgetText).observe(budgetTextSrc, { childList: true, subtree: true, characterData: true });
    }

    // Mobile budget button: open settings costs tab
    const mobileBudgetAction = () => {
        closeNavDrawer();
        openDashboardTab('costs');
    };
    document.getElementById('mobile-budget-row-btn')?.addEventListener('click', mobileBudgetAction);
    document.getElementById('mobile-budget-pill-mirror')?.addEventListener('click', mobileBudgetAction);

    // Sync budget bar width from #chat-budget-bar-fill to #mobile-budget-fill-mirror
    const budgetFillSrc = document.getElementById('chat-budget-bar-fill');
    const budgetFillMirror = document.getElementById('mobile-budget-fill-mirror');
    if (budgetFillSrc && budgetFillMirror) {
        const syncFill = () => { budgetFillMirror.style.width = budgetFillSrc.style.width; };
        syncFill();
        new MutationObserver(syncFill).observe(budgetFillSrc, { attributes: true, attributeFilter: ['style'] });
    }

    // Sync version text from #nav-version to #mobile-version-mirror
    const versionSrc = document.getElementById('nav-version');
    const versionMirror = document.getElementById('mobile-version-mirror');
    if (versionSrc && versionMirror) {
        const syncVersion = () => { versionMirror.textContent = versionSrc.textContent; };
        syncVersion();
        new MutationObserver(syncVersion).observe(versionSrc, { childList: true, subtree: true, characterData: true });
    }

    // Sync on/off state of toggle commands (review, bg) from desktop buttons
    function syncCommandToggleState() {
        ['review', 'bg'].forEach((cmd) => {
            const desktop = document.querySelector(`#chat-header-actions [data-chat-command="${cmd}"]`);
            const mobile = document.querySelector(`[data-mobile-cmd="${cmd}"]`);
            if (desktop && mobile) {
                mobile.classList.toggle('on', desktop.classList.contains('on'));
            }
        });
    }
    // Re-sync toggle states when mobile drawer opens
    navOpenBtn?.addEventListener('click', syncCommandToggleState);
    window.addEventListener('ouro:page-shown', syncCommandToggleState);
}());
