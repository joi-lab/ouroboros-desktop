import {
    clearPending,
    getPending,
    getPendingBySlug,
    lifecycleCardClassFor,
    lifecycleSpinnerFor,
    setPending,
    startLifecyclePoller,
} from './lifecycle_card.js';
import {
    emitSkillLifecycle,
    escapeHtmlAttr as escapeHtml,
    fetchJson,
} from './utils.js';


function lifecycleFor(installed, pending) {
    if (pending) {
        if (pending.failed === true) {
            return {
                tone: pending.tone || 'danger',
                label: pending.label || 'Ошибка',
                hint: pending.message || '',
                button: pending.retry_label || 'Повторить',
                disabled: false,
            };
        }
        return {
            tone: pending.tone || 'warn',
            label: pending.label || 'Выполняется',
            hint: pending.message || '',
            button: pending.label || 'Выполняется…',
            disabled: true,
        };
    }
    if (installed) {
        const review = installed.review_status ? `Проверка ${installed.review_status}` : 'Установлен';
        const executable = installed.review_gate && typeof installed.review_gate.executable_review === 'boolean'
            ? installed.review_gate.executable_review
            : ['clean', 'warnings'].includes(installed.review_status);
        return {
            tone: executable && !installed.review_stale ? 'ok' : 'warn',
            label: review,
            hint: installed.review_stale ? 'Проверка устарела; перепроверьте в разделе «Мои навыки» перед включением.' : '',
            button: 'Установлен',
            disabled: true,
        };
    }
    return {
        tone: 'muted',
        label: 'Не установлен',
        hint: 'Установка автоматически запускает проверку безопасности.',
        button: 'Установить',
        disabled: false,
    };
}


function controlsTemplate() {
    return `
        <div class="marketplace-controls">
            <input type="search" id="oh-query" class="marketplace-search"
                   placeholder="Поиск официальных навыков Ouroboros…" autocomplete="off">
            <button class="btn btn-primary" data-oh-search>Поиск</button>
        </div>
    `;
}


function template({ includeControls = true } = {}) {
    return `
        <div class="marketplace-shell">
            ${includeControls ? controlsTemplate() : ''}
            <div id="oh-status" class="muted marketplace-status"></div>
            <div id="oh-results" class="marketplace-results"></div>
        </div>
    `;
}


function card(item, installed) {
    const slug = item.slug;
    const pending = getPending(slug);
    const lifecycle = lifecycleFor(installed, pending);
    const cardClass = lifecycleCardClassFor(pending);
    const spinner = lifecycleSpinnerFor(pending);
    const lifecycleHint = lifecycle.hint
        ? `<div class="marketplace-card-state-hint">${escapeHtml(lifecycle.hint)}</div>`
        : '';
    const status = installed
        ? `<span class="skills-status-chip skills-status-ok">Установлен v${escapeHtml(installed.version || item.latest_version || '')}</span>`
        : '<span class="skills-status-chip skills-status-muted">Не установлен</span>';
    const primary = (installed && !pending)
        ? '<button class="btn btn-default" disabled>Установлен</button>'
        : `<button class="btn ${pending?.failed ? 'btn-default' : 'btn-primary'}" data-oh-install="${escapeHtml(slug)}" ${lifecycle.disabled ? 'disabled' : ''}>${escapeHtml(lifecycle.button)}</button>`;
    return `
        <article class="${cardClass}" data-slug="${escapeHtml(slug)}">
            <div class="marketplace-card-head">
                <div class="marketplace-card-title">
                    <strong>${escapeHtml(item.display_name || slug)}</strong>
                    <span class="muted">${escapeHtml(slug)} · v${escapeHtml(item.latest_version || '—')}</span>
                </div>
                <div class="marketplace-card-badges">
                    <span class="skills-badge skills-badge-ok">официальный</span>
                    ${status}
                </div>
            </div>
            <div class="marketplace-card-body">${escapeHtml(item.summary || item.description || '')}</div>
            <div class="marketplace-card-state marketplace-state-${lifecycle.tone}">
                <strong>${spinner}${escapeHtml(lifecycle.label)}</strong>
                ${lifecycleHint}
            </div>
            <div class="marketplace-card-actions">
                <div class="marketplace-primary-action">${primary}</div>
                <div class="marketplace-secondary-actions">
                    <button class="btn btn-default" data-oh-preview="${escapeHtml(slug)}">Подробнее</button>
                </div>
            </div>
        </article>
    `;
}


export function initOuroborosHub(pane, controlsHost = null) {
    pane.innerHTML = template({ includeControls: !controlsHost });
    if (controlsHost) {
        controlsHost.innerHTML = controlsTemplate();
    }
    const state = { query: '', results: [], installed: new Map() };
    const controlsRoot = controlsHost || pane;
    const queryInput = controlsRoot.querySelector('#oh-query');
    const results = pane.querySelector('#oh-results');
    const status = pane.querySelector('#oh-status');

    const show = (message, tone = '') => {
        status.dataset.tone = tone;
        status.textContent = message;
    };

    function renderCards() {
        results.innerHTML = state.results.map((item) => card(item, state.installed.get(item.slug))).join('')
            || '<div class="muted">Официальные навыки не найдены.</div>';
    }

    async function loadInstalled() {
        const data = await fetchJson('/api/marketplace/ouroboroshub/installed').catch(() => ({ skills: [] }));
        state.installed = new Map((data.skills || []).map((skill) => [skill.name, skill]));
    }

    async function refresh() {
        show('Загрузка OuroborosHub…', 'muted');
        try {
            await loadInstalled();
            const params = new URLSearchParams();
            if (state.query.trim()) params.set('q', state.query.trim());
            const data = await fetchJson(`/api/marketplace/ouroboroshub/catalog?${params}`);
            state.results = data.results || [];
            state.installed.pendingBySlug = getPendingBySlug();
            renderCards();
            show(`${state.results.length} официальных навык${state.results.length === 1 ? '' : (state.results.length < 5 ? 'а' : 'ов')}`, 'muted');
        } catch (err) {
            show(err.message || String(err), 'danger');
            results.innerHTML = `<div class="skills-load-error">${escapeHtml(err.message || err)}</div>`;
        }
    }

    queryInput.addEventListener('input', (event) => {
        state.query = event.target.value || '';
        clearTimeout(pane._ohTimer);
        pane._ohTimer = setTimeout(refresh, 250);
    });
    controlsRoot.querySelector('[data-oh-search]').addEventListener('click', refresh);
    startLifecyclePoller(() => {
        state.installed.pendingBySlug = getPendingBySlug();
        renderCards();
    });
    results.addEventListener('click', async (event) => {
        const install = event.target.closest('[data-oh-install]');
        const preview = event.target.closest('[data-oh-preview]');
        if (preview) {
            const slug = preview.dataset.ohPreview;
            show(`${slug}: подробности официального навыка показаны в карточке каталога.`, 'muted');
            return;
        }
        if (!install) return;
        const slug = install.dataset.ohInstall;
        install.disabled = true;
        setPending(slug, { label: 'Установка', tone: 'warn', message: 'Установка официального навыка…' });
        show(`Установка ${slug}…`, 'muted');
        try {
            const data = await fetchJson('/api/marketplace/ouroboroshub/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slug, auto_review: true }),
            });
            if (!data.ok) throw new Error(data.error || 'install failed');
            show(
                data.review_status ? `${slug}: установлен, проверка ${data.review_status}` : `${slug}: установлен`,
                data.ok ? 'ok' : 'warn',
            );
            emitSkillLifecycle('install', data.sanitized_name || slug, data);
            clearPending(slug);
        } catch (err) {
            setPending(slug, {
                label: 'Ошибка',
                tone: 'danger',
                message: err.message || String(err),
                failed: true,
                retry_label: 'Повторить',
            });
            show(`${slug}: ${err.message || err}`, 'danger');
        } finally {
            install.disabled = false;
            refresh();
        }
    });
    pane._ouroboroshubRefresh = refresh;
    refresh();
}
