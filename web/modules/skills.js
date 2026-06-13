import { initMarketplace } from './marketplace.js';
import { initOuroborosHub } from './ouroboroshub.js';
import { renderPageHeader, renderTabStrip } from './page_header.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { PAGE_ICONS } from './page_icons.js';
import { showToast } from './toast.js';
import { apiClient, apiFetch } from './api_client.js';
import { renderInstalledSkillCard } from './skill_card_renderer.js';
import { installedTime } from './ui_helpers.js';
import {
    boundedText,
    emitSkillLifecycle,
    escapeHtmlAttr as escapeHtml,
    grantReady,
    renderSkillRepairPrompt,
    reviewTone,
    reviewReady,
} from './utils.js';

const SKILLS_TABS = [
    { value: 'installed', label: 'Мои навыки', pillId: 'skills-tab-pill-installed', pillClass: 'skills-tab-pill' },
    { value: 'marketplace', label: 'ClawHub', pillId: 'skills-tab-pill-marketplace', pillClass: 'skills-tab-pill' },
    { value: 'ouroboroshub', label: 'OuroborosHub', pillId: 'skills-tab-pill-ouroboroshub', pillClass: 'skills-tab-pill' },
];

/** Installed skills UI: review, grant, enable, repair, update, uninstall, delete. */

function skillsPageTemplate() {
    return `
        <section class="page app-page-glass" id="page-skills">
            ${renderPageHeader({
                title: 'Навыки',
                icon: PAGE_ICONS.skills,
                description: 'Навыки расширяют Ouroboros новыми инструментами, маршрутами и виджетами. Каждый навык проходит проверку безопасности перед включением.',
                actionsHtml: '<button id="skills-refresh" class="btn btn-default btn-sm">Обновить</button>',
                tabsHtml: renderTabStrip({
                    items: SKILLS_TABS,
                    active: 'installed',
                    dataAttr: 'data-tab',
                    activeClass: 'is-active',
                    ariaLabel: 'Просмотры навыков',
                    stripClass: 'skills-tabs',
                    tabClass: 'skills-tab',
                }),
            })}
            <div class="skills-search-chrome" id="skills-pane-marketplace-chrome" data-chrome-pane="marketplace" hidden></div>
            <div class="skills-search-chrome" id="skills-pane-ouroboroshub-chrome" data-chrome-pane="ouroboroshub" hidden></div>
            <div class="skills-scroll scroll-fade-y">
                <div class="skills-tab-panel" id="skills-pane-installed" data-pane="installed">
                    <div id="skills-migration-banner" class="skills-migration-banner" hidden></div>
                    <div id="skills-list" class="skills-list"></div>
                    <div id="skills-empty" class="muted" hidden>
                        Навыков пока нет. Перейдите в <b>ClawHub</b> или
                        <b>OuroborosHub</b>, чтобы добавить навык, или импортируйте
                        пакет из вкладки «Файлы».
                    </div>
                </div>
                <div class="skills-tab-panel" id="skills-pane-marketplace" data-pane="marketplace" hidden></div>
                <div class="skills-tab-panel" id="skills-pane-ouroboroshub" data-pane="ouroboroshub" hidden></div>
            </div>
        </section>
    `;
}


function isMissingGrantLoadError(skill) {
    return !grantReady(skill) && String(skill.load_error || '').includes('missing owner grants');
}

function hasSkillUiTab(skill, live = {}) {
    const tabs = Array.isArray(live?.ui_tabs) ? live.ui_tabs : [];
    return tabs.some((tab) => {
        const owner = tab?.skill || tab?.skill_name || tab?.extension || '';
        return owner === skill.name;
    });
}

// v5.2.3: collapse the previous wall of competing badges
// (NATIVE / PASS / LIVE / ENABLED / GRANT MISSING / etc.) into a single
// human-readable status chip per card. The detailed flags stay
// available under the Details disclosure for advanced operators.
function skillStatusChip(skill, live = {}) {
    if (!grantReady(skill)) {
        return { tone: 'warn', label: 'Требует доступ' };
    }
    if (skill.lifecycle_virtual && isRateLimitError(skill.load_error)) {
        return { tone: 'warn', label: 'Лимит запросов' };
    }
    if (skill.load_error) {
        return { tone: 'danger', label: 'Ошибка загрузки' };
    }
    if (!reviewReady(skill)) {
        return { tone: 'warn', label: 'Нужна проверка' };
    }
    if (skill.enabled) {
        if (skill.type === 'extension') {
            if (skill.live_loaded && (skill.dispatch_live || hasSkillUiTab(skill, live))) {
                return { tone: 'ok', label: 'Активен' };
            }
            if (skill.live_loaded && !skill.dispatch_live && !hasSkillUiTab(skill, live)) {
                return { tone: 'warn', label: 'Загружен — вкладка ожидает' };
            }
            return { tone: 'warn', label: 'Включён — не загружен' };
        }
        return { tone: 'ok', label: 'Включён' };
    }
    return { tone: 'muted', label: 'Выкл' };
}

// v5.2.3 follow-up (review): surface a calm provenance label on the
// card front face. Built-in skills carry no chip (the absence is the
// signal). Third-party / external skills get a small muted/warn pill
// next to the title so operators can tell at a glance who shipped the
// code without expanding Show details. Mirrors P1 "Provenance matters".
function skillSourceChip(skill) {
    const source = (skill.source || 'native').toLowerCase();
    if (source === 'native') {
        return '';
    }
    const labelMap = {
        clawhub: { label: 'ClawHub', tone: 'warn' },
        ouroboroshub: { label: 'OuroborosHub', tone: 'ok' },
        self_authored: { label: 'Authored', tone: 'ok' },
        external: { label: 'External', tone: 'muted' },
        user_repo: { label: 'User repo', tone: 'muted' },
    };
    const entry = labelMap[source] || { label: source, tone: 'muted' };
    return `<span class="skills-source-chip skills-source-${entry.tone}" title="Source: ${escapeHtml(entry.label)}">${escapeHtml(entry.label)}</span>`;
}

function renderReviewFindings(skill) {
    const findings = Array.isArray(skill.review_findings) ? skill.review_findings : [];
    if (!findings.length) return '';
    const rows = findings.map((finding) => {
        const item = finding.item || finding.check || finding.title || 'finding';
        const verdict = finding.verdict || finding.severity || '';
        const reason = finding.reason || finding.message || JSON.stringify(finding);
        return `<li><strong>${escapeHtml(verdict)}</strong> ${escapeHtml(item)}: ${escapeHtml(reason)}</li>`;
    }).join('');
    return `
        <details class="skills-review-findings">
            <summary class="muted">${findings.length} ${findings.length === 1 ? 'замечание' : (findings.length < 5 ? 'замечания' : 'замечаний')} по проверке</summary>
            <ul>${rows}</ul>
        </details>
    `;
}

function renderGrantBlock(skill) {
    const grants = skill.grants || {};
    const requested = Array.isArray(grants.requested_keys) ? grants.requested_keys : [];
    const requestedPermissions = Array.isArray(grants.requested_permissions) ? grants.requested_permissions : [];
    // v5.2.3: keep the affordance discoverable but quiet the copy.
    // Skills that do not request any core keys get a single muted
    // line at the bottom of the Details disclosure instead of a
    // dedicated section on the front face of the card.
    if (!requested.length && !requestedPermissions.length) {
        return '';
    }
    const missing = Array.isArray(grants.missing_keys) ? grants.missing_keys : [];
    const missingPermissions = Array.isArray(grants.missing_permissions) ? grants.missing_permissions : [];
    const granted = Array.isArray(grants.granted_keys) ? grants.granted_keys : [];
    const grantedPermissions = Array.isArray(grants.granted_permissions) ? grants.granted_permissions : [];
    const unsupported = grants.unsupported_for_skill_type === true;
    const reviewBlocked = !reviewReady(skill);

    const requestedKeysHtml = requested
        .map((key) => `<code>${escapeHtml(key)}</code>`)
        .join(' ');
    const requestedPermsHtml = requestedPermissions
        .map((key) => `<code>${escapeHtml(key)}</code>`)
        .join(' ');

    let statusLine;
    let statusTone;
    if (unsupported) {
        statusLine = 'Этот тип навыка не может получать ключи основного API.';
        statusTone = 'muted';
    } else if (!missing.length && !missingPermissions.length) {
        statusLine = 'Доступ предоставлен.';
        statusTone = 'ok';
    } else if (reviewBlocked) {
        statusLine = 'Сначала выполните проверку безопасности, затем предоставьте доступ.';
        statusTone = 'warn';
    } else {
        statusLine = 'Этот навык требует вашего разрешения для использования указанных ключей.';
        statusTone = 'warn';
    }

    const grantedRow = granted.length
        ? `<div class="skills-access-row"><span class="skills-access-label">Предоставлено</span> ${granted.map((k) => `<code>${escapeHtml(k)}</code>`).join(' ')}</div>`
        : '';

    return `
        <div class="skills-access skills-access-${statusTone}">
            <div class="skills-access-row">
                <span class="skills-access-label">Требует доступ</span>
                ${requestedKeysHtml} ${requestedPermsHtml}
            </div>
            ${grantedRow}
            ${grantedPermissions.length ? `<div class="skills-access-row"><span class="skills-access-label">Предоставленные разрешения</span> ${grantedPermissions.map((k) => `<code>${escapeHtml(k)}</code>`).join(' ')}</div>` : ''}
            <div class="skills-access-status">${escapeHtml(statusLine)}</div>
        </div>
    `;
}


function extensionLiveBadge(skill) {
    if (skill.type !== 'extension') return '';
    const pendingUiTabs = Array.isArray(skill.ui_tabs_pending) ? skill.ui_tabs_pending : [];
    if (pendingUiTabs.length && !skill.dispatch_live) {
        return '<span class="skills-badge skills-badge-warn">ui tab pending</span>';
    }
    if (skill.live_loaded && skill.dispatch_live) {
        return '<span class="skills-badge skills-badge-ok">live</span>';
    }
    if (skill.live_loaded) {
        return '<span class="skills-badge skills-badge-muted">loaded</span>';
    }
    if (skill.desired_live) {
        return '<span class="skills-badge skills-badge-warn">catalog only</span>';
    }
    return '<span class="skills-badge skills-badge-muted">not live</span>';
}


function extensionLiveNote(skill) {
    if (skill.type !== 'extension') return '';
    const pendingUiTabs = Array.isArray(skill.ui_tabs_pending) ? skill.ui_tabs_pending : [];
    if (pendingUiTabs.length && !skill.dispatch_live) {
        return '<div class="muted">extension runtime: ui tab declared, but the browser host does not ship extension tabs yet</div>';
    }
    const reason = escapeHtml(skill.live_reason || 'catalog_only');
    const prefix = skill.live_loaded && skill.dispatch_live
        ? 'extension runtime: live'
        : (skill.live_loaded ? 'extension runtime: loaded' : 'extension runtime');
    return `<div class="muted">${prefix}${skill.live_loaded && skill.dispatch_live ? '' : ` (${reason})`}</div>`;
}


function renderProvenanceBlock(prov) {
    if (!prov || typeof prov !== 'object') return '';
    const rows = [];
    if (prov.slug) {
        rows.push(`<span>slug: <code>${escapeHtml(prov.slug)}</code></span>`);
    }
    if (prov.sha256) {
        rows.push(`<span>sha256: <code>${escapeHtml(String(prov.sha256).slice(0, 12))}…</code></span>`);
    }
    if (prov.license) {
        rows.push(`<span>license: ${escapeHtml(prov.license)}</span>`);
    }
    const homepageHref = safeExternalUrl(prov.homepage);
    if (homepageHref) {
        rows.push(`<a href="${homepageHref}" target="_blank" rel="noopener noreferrer">homepage</a>`);
    }
    if (prov.registry_url) {
        rows.push(`<span>registry: <code>${escapeHtml(prov.registry_url)}</code></span>`);
    }
    const meta = rows.length ? `<div class="skills-card-provenance muted">${rows.join(' · ')}</div>` : '';
    const warnings = Array.isArray(prov.adapter_warnings) ? prov.adapter_warnings : [];
    const warningsBlock = warnings.length
        ? `<details class="skills-card-warnings">
             <summary class="muted">${warnings.length} ${warnings.length === 1 ? 'предупреждение' : (warnings.length < 5 ? 'предупреждения' : 'предупреждений')} адаптера</summary>
             <ul>${warnings.map((msg) => `<li>${escapeHtml(msg)}</li>`).join('')}</ul>
           </details>`
        : '';
    return meta + warningsBlock;
}


function installTimestamp(skill) {
    const raw = skill.installed_at || skill.provenance?.installed_at || skill.provenance?.updated_at || '';
    const time = Date.parse(raw);
    return Number.isFinite(time) ? time : 0;
}

function installedAgo(skill) {
    const time = installTimestamp(skill);
    if (!time) return '';
    const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
    if (seconds < 90) return 'Только что установлен';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 90) return `${minutes} мин. назад`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours} ч. назад`;
    const days = Math.floor(hours / 24);
    if (days < 45) return `${days} дн. назад`;
    const date = new Date(time);
    return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function sortSkillsForDisplay(skills) {
    return [...skills].sort((a, b) => {
        if (a.lifecycle_virtual && !b.lifecycle_virtual) return -1;
        if (!a.lifecycle_virtual && b.lifecycle_virtual) return 1;
        return installedTime(b) - installedTime(a) || String(a.name || '').localeCompare(String(b.name || ''));
    });
}


function toggleLockReason(skill) {
    // Enable transitions are locked unless the skill has a fresh executable review.
    // The server enforces the same gate in ``api_skill_toggle``; this UI guard
    // keeps stale/review/repair work as explicit actions instead of hiding them
    // behind the toggle.
    if (skill?.review_gate && skill.review_gate.executable_review === false) {
        return skill.review_gate.summary || skill.review_gate.blocking_reason || 'review has not produced an executable verdict yet';
    }
    if (skill.review_status === 'blockers' && !reviewReady(skill)) return 'review has blocker findings — repair the skill first';
    if (skill.review_stale) return 'review is stale — re-review the skill first';
    if (skill.review_status === 'pending') return 'review is still pending';
    if (!reviewReady(skill)) return 'review has not produced an executable verdict yet';
    if (skill.load_error && !isMissingGrantLoadError(skill)) return 'load error — repair the skill first';
    return '';
}

function skillNextAction(skill, reviewInProgress = false, repairInProgress = false, live = {}) {
    if (reviewInProgress) {
        return { label: 'Проверяется...', className: '', disabled: true };
    }
    if (repairInProgress) {
        return { label: 'Восстанавливается...', className: '', disabled: true };
    }
    if (skill.lifecycle_virtual && skill.source === 'clawhub' && isRateLimitError(skill.load_error)) {
        return { label: 'Повторить установку', className: 'skills-retry-install', disabled: false };
    }
    if ((skill.load_error && !isMissingGrantLoadError(skill)) || (skill.review_status === 'blockers' && !reviewReady(skill))) {
        if (healReady(skill)) {
            return { label: 'Восстановить', className: 'skills-heal', disabled: false };
        }
        return { label: '', className: '', disabled: true };
    }
    if (healReady(skill)) {
        return { label: 'Восстановить', className: 'skills-heal', disabled: false };
    }
    if (skill.enabled && skill.type === 'extension' && skill.live_loaded && hasSkillUiTab(skill, live)) {
        return { label: 'Открыть виджеты', className: 'skills-open-widgets', disabled: false };
    }
    return { label: '', className: '', disabled: true };
}

function getSkillPrimaryAction(skill, reviewInProgress = false, repairInProgress = false, live = {}) {
    if (reviewInProgress) {
        return { action: '', label: 'Проверяется...', disabled: true };
    }
    if (repairInProgress) {
        return { action: '', label: 'Восстанавливается...', disabled: true };
    }
    if ((skill.load_error && !isMissingGrantLoadError(skill)) || (skill.review_status === 'blockers' && !reviewReady(skill))) {
        if (healReady(skill)) {
            return { action: 'repair', label: 'Восстановить', danger: true };
        }
        return { action: '', label: '', disabled: true };
    }
    if (!reviewReady(skill)) {
        return {
            action: skill.review_stale ? 'rereview' : 'review',
            label: skill.review_stale ? 'Перепроверить' : 'Проверить',
        };
    }
    if (skill.is_self_authored && !skill.enabled) {
        const grants = skill.grants || {};
        const keys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
        const permissions = Array.isArray(grants.missing_permissions)
            ? grants.missing_permissions
            : (grants.requested_permissions || []);
        return { action: 'approve_enable', label: 'Одобрить и включить', keys: [...keys, ...permissions].join(',') };
    }
    if (!grantReady(skill)) {
        const grants = skill.grants || {};
        const keys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
        const permissions = Array.isArray(grants.missing_permissions)
            ? grants.missing_permissions
            : (grants.requested_permissions || []);
        return { action: 'grant', label: 'Предоставить доступ', keys: [...keys, ...permissions].join(',') };
    }
    if (skill.enabled && skill.type === 'extension' && skill.live_loaded && hasSkillUiTab(skill, live)) {
        return { action: 'open_widgets', label: 'Открыть виджеты' };
    }
    return { action: '', label: '' };
}

function renderSkillCard(skill, reviewingSkills = new Set(), repairingSkills = new Set(), live = {}, options = {}) {
    const safeName = escapeHtml(skill.name);
    const description = escapeHtml(skill.description || '');
    const installedVersion = skill.version || '—';
    const reviewInProgress = reviewingSkills.has(skill.name);
    const repairInProgress = repairingSkills.has(skill.name);

    const lockReason = toggleLockReason(skill);
    const primaryAction = getSkillPrimaryAction(skill, reviewInProgress, repairInProgress, live);
    const actionAttrs = primaryAction.action
        ? `data-skill="${safeName}" data-skill-action="${escapeHtml(primaryAction.action)}" role="button" tabindex="0"`
        : '';
    // v5.2.2/3: enable transitions are locked by review + grant gates.
    // Disable transitions stay clickable so an owner can always pull
    // a misbehaving skill offline even if its review goes stale.
    const toggleLocked = !skill.enabled && Boolean(lockReason);
    // v5.2.3 review-cycle fix: use the skill name as the accessible
    // name and ``role="switch"`` so AT users hear "weather, on, switch"
    // instead of the awkward "Disable weather, checked, checkbox".
    const toggleAriaLabel = toggleLocked
        ? `${skill.name} (locked: ${lockReason})`
        : skill.name;

    const status = skillStatusChip(skill, live);
    const statusChip = `<span class="skills-status-chip skills-status-${status.tone} ${primaryAction.action ? 'is-clickable' : ''}" ${actionAttrs}>${escapeHtml(status.label)}</span>`;
    const sourceChip = skillSourceChip(skill);
    const installedLabel = installedAgo(skill);

    const toggleActionAttrs = toggleLocked && primaryAction.action
        ? `data-skill="${safeName}" data-skill-action="${escapeHtml(primaryAction.action)}"`
        : '';
    const toggleSwitch = skill.lifecycle_virtual ? '' : `
        <label class="skills-switch ${toggleLocked ? 'is-locked' : ''}" ${toggleActionAttrs} title="${escapeHtml(toggleLocked ? `Заблокировано: ${lockReason}` : (skill.enabled ? 'Выключить навык' : 'Включить навык'))}">
            <input type="checkbox"
                   class="skills-toggle"
                   role="switch"
                   data-skill="${safeName}"
                   ${skill.enabled ? 'checked' : ''}
                   ${toggleLocked ? 'disabled' : ''}
                   aria-checked="${skill.enabled ? 'true' : 'false'}"
                   aria-label="${escapeHtml(toggleAriaLabel)}">
            <span class="skills-switch-track" aria-hidden="true">
                <span class="skills-switch-thumb"></span>
            </span>
        </label>
    `;

    const lockHint = toggleLocked
        ? `<div class="skills-lock-hint ${primaryAction.action ? 'is-clickable' : ''}" title="${escapeHtml(lockReason)}" ${actionAttrs}>Заблокировано: ${escapeHtml(lockReason)}</div>`
        : '';
    const reviewProgress = reviewInProgress
        ? `
            <div class="skills-review-progress" role="status" aria-live="polite">
                <span class="skills-review-spinner" aria-hidden="true"></span>
                <span>Проверка выполняется</span>
            </div>
        `
        : '';
    const repairProgress = repairInProgress
        ? `
            <div class="skills-review-progress skills-repair-progress" role="status" aria-live="polite">
                <span class="skills-review-spinner" aria-hidden="true"></span>
                <span>Задача восстановления ставится в очередь</span>
            </div>
        `
        : '';

    const missingGrantError = isMissingGrantLoadError(skill);
    const loadError = skill.load_error && !missingGrantError
        ? `<div class="skills-load-error">${escapeHtml(skill.load_error)}</div>`
        : '';

    const source = (skill.source || 'native').toLowerCase();
    const sourceLabel = source === 'clawhub' ? 'ClawHub'
        : source === 'ouroboroshub' ? 'OuroborosHub'
        : source === 'self_authored' ? 'Authored'
        : source === 'native' ? 'Built-in'
        : source === 'external' ? 'External'
        : source === 'user_repo' ? 'User repo'
        : source;

    const isMarketplaceManaged = source === 'clawhub' || source === 'ouroboroshub';
    const provenance = isMarketplaceManaged ? skill.provenance : null;
    const updateBtn = isMarketplaceManaged
        ? `<button type="button" role="menuitem" class="skills-menu-item skills-update" data-skill="${safeName}" data-source="${escapeHtml(source)}">Обновить</button>`
        : '';
    const uninstallBtn = isMarketplaceManaged
        ? `<button type="button" role="menuitem" class="skills-menu-item skills-uninstall" data-skill="${safeName}" data-source="${escapeHtml(source)}">Удалить</button>`
        : '';
    const healBtn = '';
    const reviewMenuBtn = !reviewInProgress
        ? `<button type="button" role="menuitem" class="skills-menu-item skills-review" data-skill="${safeName}">${skill.review_status === 'pending' ? 'Проверить' : (skill.review_stale ? 'Перепроверить' : 'Проверить снова')}</button>`
        : '';
    const submitHub = submitHubReady(skill, Boolean(options.githubTokenConfigured));
    const submitHubBtn = submitHub.visible
        ? `<button type="button" role="menuitem" class="skills-menu-item skills-submit-hub" data-skill="${safeName}" ${submitHub.disabled ? 'disabled' : ''} title="${escapeHtml(submitHub.reason)}">Отправить в OuroborosHub</button>`
        : '';
    const next = skillNextAction(skill, reviewInProgress, repairInProgress, live);
    const nextAttrs = [
        `data-skill="${safeName}"`,
        next.keys ? `data-keys="${escapeHtml(next.keys)}"` : '',
        next.enabled ? `data-enabled="${escapeHtml(next.enabled)}"` : '',
        next.disabled ? 'disabled' : '',
    ].filter(Boolean).join(' ');
    const nextButton = next.label ? `
        <button class="btn btn-primary skills-next-action ${escapeHtml(next.className)}" ${nextAttrs}>
            ${escapeHtml(next.label)}
        </button>
    ` : '';
    const primaryButton = primaryAction.action ? `
        <button type="button"
                class="btn btn-primary skills-primary-action"
                data-skill="${safeName}"
                data-skill-action="${escapeHtml(primaryAction.action)}"
                ${primaryAction.keys ? `data-keys="${escapeHtml(primaryAction.keys)}"` : ''}
                ${primaryAction.disabled ? 'disabled' : ''}>
            ${escapeHtml(primaryAction.label)}
        </button>
    ` : '';

    // v5.2.3 review-cycle fix: review findings are a primary safety
    // signal (P3). Promote the disclosure out of "Show details" so a
    // user with a fail/advisory verdict sees the count one click
    // away from the front face, not two.
    const reviewFindings = renderReviewFindings(skill);

    // Detail disclosure — power-user metadata only.
    const permissions = (skill.permissions || [])
        .map((p) => `<code>${escapeHtml(p)}</code>`)
        .join(' ');
    const provenanceVersion = provenance?.version || '';
    const versionDrift = (provenanceVersion && provenanceVersion !== installedVersion)
        ? `<div class="skills-detail-row"><span class="skills-detail-label">Расхождение версий</span> манифест ${escapeHtml(installedVersion)} vs реестр ${escapeHtml(provenanceVersion)}</div>`
        : '';
    const liveLine = (skill.type === 'extension' && skill.live_loaded && hasSkillUiTab(skill, live))
        ? `<div class="skills-detail-row"><span class="skills-detail-label">Визуальные виджеты</span> доступны на вкладке «Виджеты»</div>`
        : '';
    const provenanceBlock = renderProvenanceBlock(provenance);
    const detailsBody = `
        <div class="skills-detail-row">
            <span class="skills-detail-label">Тип</span>
            <code>${escapeHtml(skill.type || 'skill')}</code> · версия ${escapeHtml(installedVersion)} · источник ${escapeHtml(sourceLabel)}
        </div>
        <div class="skills-detail-row">
            <span class="skills-detail-label">Проверка</span>
            ${statusBadge(skill.review_status, skill.review_gate)}${skill.review_stale ? ' <span class="skills-badge skills-badge-warn">stale</span>' : ''}
        </div>
        <div class="skills-detail-row">
            <span class="skills-detail-label">Разрешения</span>
            ${permissions || '<i class="muted">нет</i>'}
        </div>
        ${versionDrift}
        ${liveLine}
        ${provenanceBlock}
    `;
    const details = `
        <details class="skills-details">
            <summary>Подробнее</summary>
            ${detailsBody}
        </details>
    `;

    // v5.7.0 kebab placement: the "more actions" menu (Re-review / Update /
    // Uninstall) lives in the card HEADER cluster (after the toggle switch),
    // which is where users hunt for "kebab" affordances per Material 3
    // / Apple HIG conventions. The popup is a non-modal <dialog> opened
    // with .show() (not .showModal()) so it appears as an anchored popover
    // under the trigger instead of as a centered viewport modal that
    // dimmed the rest of the page.
    const cardMenu = (updateBtn || uninstallBtn || reviewMenuBtn || submitHubBtn)
        ? `
                    <div class="skills-card-menu">
                        <button type="button" class="skills-card-menu-trigger" aria-label="Дополнительные действия" aria-haspopup="menu" aria-expanded="false" data-skill-menu-trigger>⋮</button>
                        <dialog class="skills-card-menu-dialog" role="menu">
                            ${reviewMenuBtn}
                            ${submitHubBtn}
                            ${updateBtn}
                            ${uninstallBtn}
                        </dialog>
                    </div>
                `
        : '';
    return `
        <article class="skills-card" data-skill="${safeName}" ${reviewInProgress ? 'data-reviewing="1"' : ''} ${repairInProgress ? 'data-repairing="1"' : ''}>
            <header class="skills-card-head">
                <div class="skills-card-title">
                    <h3>${safeName}${sourceChip ? ` ${sourceChip}` : ''}</h3>
                    ${description ? `<p class="skills-card-desc">${description}</p>` : ''}
                    ${installedLabel ? `<div class="skills-card-installed muted">${escapeHtml(installedLabel)}</div>` : ''}
                </div>
                <div class="skills-card-toggle">
                    ${statusChip}
                    ${primaryButton || nextButton}
                    ${toggleSwitch}
                    ${cardMenu}
                </div>
            </header>
            ${lockHint}
            ${reviewProgress}
            ${repairProgress}
            ${renderGrantBlock(skill)}
            ${reviewFindings}
            ${loadError}
            <footer class="skills-card-actions">
                ${healBtn}
                ${details}
            </footer>
        </article>
    `;
}


async function fetchSkills() {
    const [stateResp, extResp, queueResp] = await Promise.all([
        apiClient.state().catch(() => ({})),
        apiClient.extensions().catch(() => ({ skills: [], live: {} })),
        apiClient.skillLifecycleQueue().catch(() => ({ events: [] })),
    ]);
    // Per-skill state is synthesized from extensions + lifecycle queue.
    const skillsRepoConfigured = Boolean(stateResp.skills_repo_configured);
    const githubTokenConfigured = Boolean(stateResp.github_token_configured);
    return {
        skillsRepoConfigured,
        githubTokenConfigured,
        skills: mergeLifecycleEvents(extResp.skills || [], queueResp.events || []),
        live: extResp.live || {},
        queue: queueResp,
    };
}


function mergeLifecycleEvents(skills, events) {
    const out = [...skills];
    const names = new Set(out.map((skill) => skill.name));
    for (const event of [...events].reverse()) {
        if (!['queued', 'running', 'failed'].includes(event.status)) continue;
        const name = event.target;
        if (!name || names.has(name)) continue;
        names.add(name);
        out.unshift({
            name,
            description: event.message || event.error || 'Skill lifecycle operation',
            version: '—',
            type: 'skill',
            enabled: false,
            review_status: 'pending',
            review_stale: true,
            permissions: [],
            load_error: event.status === 'failed' ? event.error : '',
            source: event.source || 'external',
            lifecycle_kind: event.kind || '',
            lifecycle_virtual: true,
            grants: { all_granted: true },
        });
    }
    updateQueueBadges(events);
    return out;
}


function updateQueueBadges(events) {
    const actionable = events.filter((event) => ['queued', 'running', 'failed'].includes(event.status));
    const bySource = new Map();
    for (const event of actionable) {
        const source = event.source === 'ouroboroshub' ? 'ouroboroshub'
            : event.source === 'clawhub' ? 'marketplace'
            : 'installed';
        bySource.set(source, (bySource.get(source) || 0) + 1);
    }
    for (const [id, count] of bySource.entries()) {
        const el = document.getElementById(`skills-tab-pill-${id}`);
        if (!el) continue;
        el.hidden = !count;
        el.textContent = count ? String(count) : '';
    }
    for (const id of ['installed', 'marketplace', 'ouroboroshub']) {
        if (bySource.has(id)) continue;
        const el = document.getElementById(`skills-tab-pill-${id}`);
        if (!el) continue;
        el.hidden = true;
        el.textContent = '';
    }
}


async function renderSkillsList(container, emptyEl, reviewingSkills = new Set(), repairingSkills = new Set()) {
    const { skillsRepoConfigured, githubTokenConfigured, skills, live } = await fetchSkills();
    if (!skills.length && !skillsRepoConfigured) {
        container.innerHTML = '';
        if (emptyEl) emptyEl.hidden = false;
        return;
    }
    if (emptyEl) emptyEl.hidden = true;
    container.innerHTML = sortSkillsForDisplay(skills).map((skill) => renderInstalledSkillCard(
        skill,
        reviewingSkills,
        repairingSkills,
        live,
        { githubTokenConfigured },
    )).join('')
        || '<div class="muted">Навыков пока нет. Добавьте из <b>ClawHub</b> или <b>OuroborosHub</b>.</div>';
    // v5: surface unread native-skill upgrade migrations so the
    // operator is told when the launcher silently rewrote an
    // installed skill (e.g. weather 0.1 script -> 0.2 extension).
    // Idempotent on re-render — we replace the top banner each pass.
    renderMigrationBanner();
}


async function renderMigrationBanner() {
    const host = document.getElementById('skills-migration-banner');
    if (!host) return;
    let migrations = [];
    try {
        const resp = await apiFetch('/api/migrations');
        if (resp.ok) {
            const data = await resp.json();
            migrations = Array.isArray(data.migrations) ? data.migrations : [];
        }
    } catch {
        // network error — leave the banner empty.
    }
    if (!migrations.length) {
        host.innerHTML = '';
        host.hidden = true;
        return;
    }
    host.hidden = false;
    host.innerHTML = migrations.map((m) => {
        const safeKey = escapeHtml(String(m.key || ''));
        const skill = escapeHtml(String(m.skill || ''));
        const oldV = escapeHtml(String(m.old_version || ''));
        const newV = escapeHtml(String(m.new_version || ''));
        const summary = escapeHtml(String(m.summary || ''));
        const ts = escapeHtml(String(m.applied_at || ''));
        return `
            <div class="skills-migration-banner-item" data-migration-key="${safeKey}">
                <div class="skills-migration-banner-text">
                    <strong>Обновление встроенного навыка:</strong> ${skill} ${oldV ? `(${oldV} → ${newV})` : `(→ ${newV})`}
                    <span class="muted"> · ${ts}</span>
                    <div class="muted">${summary}</div>
                </div>
                <button class="btn btn-default skills-migration-dismiss" data-key="${safeKey}">Понятно</button>
            </div>
        `;
    }).join('');
    // v5 Cycle 2 Gemini Finding 1 + Opus C2-2: attach the dismiss
    // listener exactly once per host element. The previous version
    // used ``{ once: true }`` which removed the listener on the FIRST
    // click anywhere inside the host — including click on the body
    // text — so subsequent clicks on the actual "Got it" button (or
    // a second migration's button) silently no-op'd. We gate the
    // listener attachment via a dataset flag instead, so each
    // re-render of the banner does NOT re-register, and ANY click
    // is delegated to the right button via ``closest()``.
    if (host.dataset.bannerListenerAttached !== '1') {
        host.dataset.bannerListenerAttached = '1';
        host.addEventListener('click', async (event) => {
            const btn = event.target.closest('.skills-migration-dismiss');
            if (!btn) return;
            const key = btn.dataset.key;
            if (!key) return;
            btn.disabled = true;
            try {
                await apiFetch(`/api/migrations/${encodeURIComponent(key)}/dismiss`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                const item = btn.closest('.skills-migration-banner-item');
                if (item) item.remove();
                if (!host.querySelector('.skills-migration-banner-item')) {
                    host.hidden = true;
                }
            } catch {
                btn.disabled = false;
            }
        });
    }
}


async function postWithFeedback(url, body) {
    const resp = await apiFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    const payload = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(payload.error || `HTTP ${resp.status}`);
    }
    return payload;
}

function buildHealPrompt(skill) {
    const findings = Array.isArray(skill.review_findings) ? skill.review_findings : [];
    const diagnostics = {
        name: boundedText(skill.name, 200),
        source: boundedText(skill.source || 'unknown', 80),
        payload_root: boundedText(skill.payload_root || '', 300),
        type: boundedText(skill.type || 'unknown', 80),
        review_status: boundedText(skill.review_status || 'pending', 80),
        review_stale: Boolean(skill.review_stale),
        load_error: boundedText(skill.load_error || 'none', 2000),
        review_findings: findings.slice(0, 12).map((finding) => ({
            item: boundedText(finding.item || finding.check || finding.title || 'finding', 200),
            verdict: boundedText(finding.verdict || finding.severity || '', 80),
            reason: boundedText(finding.reason || finding.message || JSON.stringify(finding), 1200),
        })),
    };
    return renderSkillRepairPrompt(
        'Repair the installed Ouroboros skill selected in the Skills UI.',
        JSON.stringify(diagnostics, null, 2),
    );
}


function attachActionHandlers(container, renderFn, reviewingSkills, repairingSkills, ctx = {}) {
    function closeSkillMenus(exceptMenu = null) {
        container.querySelectorAll('.skills-card-menu').forEach((menu) => {
            if (menu === exceptMenu) return;
            const popover = menu.querySelector('.skills-card-menu-dialog');
            const trigger = menu.querySelector('[data-skill-menu-trigger]');
            if (popover?.open) popover.close();
            if (trigger) trigger.setAttribute('aria-expanded', 'false');
        });
    }

    async function requestMissingKeyGrants(name, items) {
        const cleanItems = (items || []).map((k) => String(k || '').trim()).filter(Boolean);
        if (!cleanItems.length) return;
        const ok = await openConfirmDialog({
            title: `Предоставить доступ для ${name}`,
            body: `Предоставить ${name} доступ к этим ключам и разрешениям?\n\n${cleanItems.join('\n')}\n\nВыдавайте доступ только проверенным навыкам, которым вы доверяете.`,
            confirmLabel: 'Предоставить доступ',
        });
        if (!ok) throw new Error('Skill grant cancelled.');
        const bridge = window.pywebview?.api?.request_skill_key_grant;
        const result = bridge
            ? await bridge(name, cleanItems)
            : await apiClient.skillGrants(name, cleanItems);
        if (!result?.ok) {
            throw new Error(result?.error || 'Skill grant was cancelled.');
        }
        return result;
    }

    async function triggerSkillAction(name, action, options = {}) {
        if (!name || !action) return;
        if (action === 'open_widgets') {
            document.querySelector('.nav-btn[data-page="widgets"]')?.click();
            return;
        }
        const { skills } = await fetchSkills();
        const skill = (skills || []).find((item) => item.name === name);
        if (!skill) throw new Error('Skill not found in current catalogue.');

        if (action === 'retry_install') {
            showToast(`${name}: retrying ClawHub install (this may take ~30s)`, 'muted');
            const result = await postWithFeedback('/api/marketplace/clawhub/install', {
                slug: name,
                overwrite: true,
                auto_review: true,
            });
            const tail = result.review_status ? ` — review ${result.review_status}` : '';
            showToast(
                result.ok
                    ? `${name}: install retried${tail}`
                    : `${name}: install retry failed — ${result.error || 'unknown'}`,
                result.ok ? 'ok' : 'danger',
            );
            if (result.ok) emitSkillLifecycle('retry_install', name, result);
            return;
        }

        if (action === 'review' || action === 'rereview') {
            const ok = await openConfirmDialog({
                title: action === 'rereview' ? `Перепроверить ${name}` : `Проверить ${name}`,
                body: `Запустить проверку безопасности для ${name}? Это может занять несколько минут и выполняется в фоне.`,
                confirmLabel: action === 'rereview' ? 'Перепроверить' : 'Запустить проверку',
            });
            if (!ok) return;
            await reviewSkillInBackground(name);
            return;
        }

        if (action === 'grant') {
            const grants = skill.grants || {};
            const keys = (options.keys || '').split(',').map((k) => k.trim()).filter(Boolean);
            const missingKeys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
            const missingPermissions = Array.isArray(grants.missing_permissions) ? grants.missing_permissions : (grants.requested_permissions || []);
            const missing = keys.length ? keys : [...missingKeys, ...missingPermissions];
            const result = await requestMissingKeyGrants(name, missing);
            if (result) {
                showToast(`${name}: requested grants saved`, 'ok');
                emitSkillLifecycle('grant', name, result);
            }
            return;
        }

        if (action === 'approve_enable') {
            const grants = skill.grants || {};
            const keys = (options.keys || '').split(',').map((k) => k.trim()).filter(Boolean);
            const missingKeys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
            const missingPermissions = Array.isArray(grants.missing_permissions) ? grants.missing_permissions : (grants.requested_permissions || []);
            const missing = keys.length ? keys : [...missingKeys, ...missingPermissions];
            if (missing.length) await requestMissingKeyGrants(name, missing);
            await toggleSkillEnabled(name, true);
            return;
        }

        if (action === 'repair') {
            if (repairingSkills.has(name)) {
                showToast(`${name}: repair is already being queued`, 'muted');
                return;
            }
            const ok = await openConfirmDialog({
                title: `Восстановить ${name}`,
                body: `Отправить задачу восстановления для ${name} в Ouroboros? Агент займётся навыком в чате.`,
                confirmLabel: 'Начать восстановление',
                danger: true,
            });
            if (!ok) return;
            repairingSkills.add(name);
            renderFn();
            try {
                const prompt = buildHealPrompt(skill);
                await postWithFeedback('/api/command', {
                    cmd: prompt,
                    task_constraint: { mode: 'skill_repair', skill_name: skill.name || name, payload_root: skill.payload_root || '', allow_enable: false, allow_review: true },
                    visible_text: `Repair task queued for ${name}. Ouroboros will inspect the skill payload and re-run review.`,
                    visible_task_id: `skill_repair_${name}`,
                });
                showToast(`${name}: repair task sent to Ouroboros`, 'ok');
                emitSkillLifecycle('repair', name);
                if (typeof ctx.showPage === 'function') {
                    ctx.showPage('chat');
                } else {
                    document.querySelector('.nav-btn[data-page="chat"]')?.click();
                }
            } finally {
                repairingSkills.delete(name);
                renderFn();
            }
            return;
        }

        if (action === 'submit_hub') {
            const ok = await openConfirmDialog({
                title: `Отправить ${name} в OuroborosHub`,
                body: `Открыть публичный pull request на GitHub для отправки ${name} в OuroborosHub? PR будет содержать проверенный пакет навыка и обновлённую запись в каталоге.`,
                confirmLabel: 'Отправить в OuroborosHub',
                danger: true,
            });
            if (!ok) return;
            const message = `Submit skill ${name} to OuroborosHub`;
            await postWithFeedback('/api/command', {
                cmd: message,
                visible_text: `Submission task queued for ${name}. Ouroboros will open a PR to OuroborosHub if validation passes.`,
                visible_task_id: `skill_submit_${name}`,
            });
            showToast(`${name}: submission task sent to Ouroboros`, 'ok');
            emitSkillLifecycle('submit_hub', name);
            if (typeof ctx.showPage === 'function') {
                ctx.showPage('chat');
            } else {
                document.querySelector('.nav-btn[data-page="chat"]')?.click();
            }
        }
    }

    async function toggleSkillEnabled(name, wantsEnabled) {
        const result = await postWithFeedback(
            `/api/skills/${encodeURIComponent(name)}/toggle`,
            { enabled: wantsEnabled }
        );
        const actionLabels = {
            extension_loaded: 'live',
            extension_unloaded: 'stopped',
            extension_already_live: '',
            extension_inactive: '',
            extension_load_error: 'load failed',
        };
        const friendlyAction = actionLabels[result.extension_action];
        const tail = friendlyAction ? ` — ${friendlyAction}` : '';
        showToast(`${name} ${wantsEnabled ? 'turned on' : 'turned off'}${tail}`, 'ok');
        emitSkillLifecycle(wantsEnabled ? 'enable' : 'disable', name, result);
        return result;
    }

    async function reviewSkillInBackground(name) {
        if (reviewingSkills.has(name)) return null;
        reviewingSkills.add(name);
        renderFn();
        try {
            showToast(`${name}: security review started; this can take a few minutes`, 'muted');
            const result = await postWithFeedback(
                `/api/skills/${encodeURIComponent(name)}/review`,
                {}
            );
            const findings = result.findings?.length ?? 0;
            const errorTail = result.error ? ` — ${result.error}` : '';
            showToast(
                `${name}: review ${result.status}${findings ? ` (${findings} findings)` : ''}${errorTail}`,
                reviewTone(result.status, result.error)
            );
            emitSkillLifecycle('review', name, result);
            return result;
        } finally {
            reviewingSkills.delete(name);
            renderFn();
        }
    }

    // Checkbox toggle uses change so keyboard and mouse activation match.
    container.addEventListener('change', async (event) => {
        const target = event.target;
        if (!target || !target.classList || !target.classList.contains('skills-toggle')) {
            return;
        }
        const name = target.dataset.skill;
        if (!name) return;
        const wantsEnabled = Boolean(target.checked);
        target.disabled = true;
        try {
            if (wantsEnabled) {
                let current = (await fetchSkills()).skills.find((skill) => skill.name === name);
                if (!current) throw new Error('Skill not found in current catalogue.');
                if ((current.review_status === 'blockers' && !reviewReady(current)) || (current.load_error && !isMissingGrantLoadError(current))) {
                    throw new Error('Repair this skill before enabling it.');
                }
                if (!reviewReady(current)) {
                    throw new Error('Run review and wait for a fresh executable review before enabling this skill.');
                }
                if (!grantReady(current)) {
                    const grants = current.grants || {};
                    const missingKeys = Array.isArray(grants.missing_keys) ? grants.missing_keys : (grants.requested_keys || []);
                    const missingPermissions = Array.isArray(grants.missing_permissions) ? grants.missing_permissions : (grants.requested_permissions || []);
                    const missing = [...missingKeys, ...missingPermissions];
                    await requestMissingKeyGrants(name, missing);
                }
            }
            await toggleSkillEnabled(name, wantsEnabled);
            target.setAttribute('aria-checked', wantsEnabled ? 'true' : 'false');
        } catch (err) {
            // Roll back to server-truth state on failed enable/disable.
            target.checked = !wantsEnabled;
            target.setAttribute('aria-checked', (!wantsEnabled).toString());
            showToast(`${name}: ${err.message || err}`, (err.message || '').includes('cancel') ? 'warn' : 'danger');
        } finally {
            target.disabled = false;
            renderFn();
        }
    });
    container.addEventListener('keydown', (event) => {
        const actionTarget = event.target.closest?.('[data-skill-action]');
        if (!actionTarget) return;
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        actionTarget.click();
    });
    container.addEventListener('click', async (event) => {
        const menuTrigger = event.target.closest('[data-skill-menu-trigger]');
        if (menuTrigger) {
            const menu = menuTrigger.closest('.skills-card-menu');
            const popover = menu?.querySelector('.skills-card-menu-dialog');
            const opening = !popover?.open;
            closeSkillMenus(opening ? menu : null);
            if (popover && menu) {
                menuTrigger.setAttribute('aria-expanded', opening ? 'true' : 'false');
                // Non-modal anchored popover; outside handlers close it.
                if (opening) popover.show();
                else popover.close();
            }
            return;
        }
        if (event.target.closest('[data-skill-menu-close]')) {
            closeSkillMenus();
            return;
        }
        const actionTarget = event.target.closest('[data-skill-action]');
        if (actionTarget) {
            const name = actionTarget.dataset.skill;
            const action = actionTarget.dataset.skillAction;
            if (action === 'repair' && repairingSkills.has(name)) {
                return;
            }
            actionTarget.disabled = true;
            try {
                await triggerSkillAction(name, action, { keys: actionTarget.dataset.keys || '' });
            } catch (err) {
                showToast(`${name}: ${err.message || err}`, (err.message || '').includes('cancel') ? 'warn' : 'danger');
            } finally {
                actionTarget.disabled = false;
                renderFn();
            }
            return;
        }
        const target = event.target.closest('button[data-skill]');
        if (!target) return;
        if (target.classList.contains('skills-toggle')) {
            // Checkbox handler above owns current toggles; ignore legacy buttons.
            return;
        }
        const name = target.dataset.skill;
        if (target.classList.contains('skills-review')) {
            if (reviewingSkills.has(name)) return;
            target.disabled = true;
            try {
                await reviewSkillInBackground(name);
            } catch (err) {
                showToast(`${name}: ${err.message || err}`, 'danger');
            } finally {
                target.disabled = false;
                renderFn();
            }
            return;
        }
        target.disabled = true;
        try {
            if (target.classList.contains('skills-next-toggle')) {
                const wantsEnabled = target.dataset.enabled === 'true';
                await toggleSkillEnabled(name, wantsEnabled);
            } else if (target.classList.contains('skills-grant')) {
                const keys = (target.dataset.keys || '').split(',').map((k) => k.trim()).filter(Boolean);
                if (!keys.length) {
                    showToast(`${name}: no requested keys or permissions to grant`, 'warn');
                } else {
                    const result = await requestMissingKeyGrants(name, keys);
                    // Grant may persist even if live extension reconcile fails.
                    const reason = result.extension_reason;
                    const action = result.extension_action;
                    const loadError = result.load_error;
                    if (reason === 'reconcile_call_failed') {
                        showToast(
                            `${name}: grant saved, but server reconcile failed \u2014 toggle disable/enable to retry`,
                            'warn'
                        );
                    } else if (loadError) {
                        showToast(
                            `${name}: grant saved, but extension load failed: ${loadError}`,
                            'warn'
                        );
                    } else if (action === 'extension_loaded') {
                        showToast(`${name}: grant saved and extension loaded`, 'ok');
                    } else {
                        showToast(`${name}: requested grants saved`, 'ok');
                    }
                }
            } else if (target.classList.contains('skills-update')) {
                const source = target.dataset.source === 'ouroboroshub' ? 'ouroboroshub' : 'clawhub';
                showToast(`${name}: updating from ${source === 'ouroboroshub' ? 'OuroborosHub' : 'ClawHub'} (this may take ~30s)`, 'muted');
                const url = source === 'ouroboroshub'
                    ? `/api/marketplace/ouroboroshub/install`
                    : `/api/marketplace/clawhub/update/${encodeURIComponent(name)}`;
                const body = source === 'ouroboroshub' ? { slug: name, overwrite: true, auto_review: true } : {};
                const result = await postWithFeedback(url, body);
                const tail = result.review_status ? ` — review ${result.review_status}` : '';
                showToast(
                    result.ok
                        ? `${name}: updated${tail}`
                        : `${name}: update failed — ${result.error || 'unknown'}`,
                    result.ok ? 'ok' : 'danger',
                );
            } else if (target.classList.contains('skills-submit-hub')) {
                if (target.dataset.submitDisabled === 'true') {
                    showToast(`${name}: submit disabled — ${target.dataset.submitReason || 'unknown reason'}`, 'warn');
                    return;
                }
                await triggerSkillAction(name, 'submit_hub');
            } else if (target.classList.contains('skills-uninstall')) {
                const source = target.dataset.source === 'ouroboroshub' ? 'ouroboroshub' : 'clawhub';
                const ok = await openConfirmDialog({
                    title: `Удалить ${name}`,
                    body: `Удалить ${name}? Это удалит data/skills/${source}/${name}/.`,
                    confirmLabel: 'Удалить',
                    danger: true,
                });
                if (!ok) {
                    return;
                }
                const url = source === 'ouroboroshub'
                    ? `/api/marketplace/ouroboroshub/uninstall/${encodeURIComponent(name)}`
                    : `/api/marketplace/clawhub/uninstall/${encodeURIComponent(name)}`;
                const result = await postWithFeedback(url, {});
                showToast(
                    result.ok ? `${name}: uninstalled` : `${name}: uninstall failed — ${result.error}`,
                    result.ok ? 'ok' : 'danger',
                );
                if (result.ok) emitSkillLifecycle('uninstall', name, result);
            } else if (target.classList.contains('skills-delete-local')) {
                const payloadRoot = target.dataset.payloadRoot || `skills/external/${name}`;
                const ok = await openConfirmDialog({
                    title: `Delete ${name}`,
                    body: `Delete ${name}? This deletes data/${payloadRoot}/ and data/state/skills/${name}/.`,
                    confirmLabel: 'Delete',
                    danger: true,
                });
                if (!ok) {
                    return;
                }
                const result = await apiClient.deleteSkill(name, payloadRoot);
                showToast(
                    result.ok ? `${name}: deleted` : `${name}: delete failed — ${result.error}`,
                    result.ok ? 'ok' : 'danger',
                );
                if (result.ok) emitSkillLifecycle('delete', name, result);
            }
        } catch (err) {
            showToast(`${name}: ${err.message || err}`, 'danger');
        } finally {
            target.disabled = false;
            closeSkillMenus();
            renderFn();
        }
    });

    document.addEventListener('click', (event) => {
        if (container.contains(event.target)) return;
        closeSkillMenus();
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeSkillMenus();
    });
    window.addEventListener('scroll', () => closeSkillMenus(), true);
}


function activateTab(tabName) {
    const buttons = document.querySelectorAll('.skills-tab');
    const panels = document.querySelectorAll('.skills-tab-panel');
    const chromeRows = document.querySelectorAll('.skills-search-chrome');
    buttons.forEach((btn) => {
        const isActive = btn.dataset.tab === tabName;
        btn.classList.toggle('is-active', isActive);
        btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });
    panels.forEach((panel) => {
        panel.hidden = panel.dataset.pane !== tabName;
    });
    chromeRows.forEach((row) => {
        row.hidden = row.dataset.chromePane !== tabName;
    });
}


async function renderMarketplacePane() {
    const pane = document.getElementById('skills-pane-marketplace');
    if (!pane) return;
    if (pane.dataset.bootstrapped === 'true') {
        // Tab entry refreshes installed state without simulating Search.
        if (typeof pane._marketplaceRefresh === 'function') {
            pane._marketplaceRefresh();
        }
        return;
    }
    pane.innerHTML = '<div class="muted">Загрузка маркетплейса…</div>';
    try {
        initMarketplace(pane, document.getElementById('skills-pane-marketplace-chrome'));
        pane.dataset.bootstrapped = 'true';
    } catch (err) {
        pane.dataset.bootstrapped = '';
        pane.innerHTML = `<div class="skills-load-error">Failed to load marketplace UI: ${escapeHtml(err.message || err)}</div>`;
        throw err;
    }
}


async function renderOuroborosHubPane() {
    const pane = document.getElementById('skills-pane-ouroboroshub');
    if (!pane) return;
    if (pane.dataset.bootstrapped === 'true') {
        if (typeof pane._ouroboroshubRefresh === 'function') {
            pane._ouroboroshubRefresh();
        }
        return;
    }
    pane.innerHTML = '<div class="muted">Загрузка OuroborosHub…</div>';
    try {
        initOuroborosHub(pane, document.getElementById('skills-pane-ouroboroshub-chrome'));
        pane.dataset.bootstrapped = 'true';
    } catch (err) {
        pane.dataset.bootstrapped = '';
        pane.innerHTML = `<div class="skills-load-error">Failed to load OuroborosHub UI: ${escapeHtml(err.message || err)}</div>`;
        throw err;
    }
}


export function initSkills(ctx) {
    const page = document.createElement('div');
    page.innerHTML = skillsPageTemplate();
    document.getElementById('content').appendChild(page.firstElementChild);

    const container = document.getElementById('skills-list');
    const emptyEl = document.getElementById('skills-empty');
    const refreshBtn = document.getElementById('skills-refresh');
    const reviewingSkills = new Set();
    const repairingSkills = new Set();

    const renderFn = async () => {
        refreshBtn.disabled = true;
        refreshBtn.classList.add('is-loading');
        const originalText = refreshBtn.textContent || 'Обновить';
        refreshBtn.textContent = 'Обновление...';
        try {
            await Promise.all([
                renderSkillsList(container, emptyEl, reviewingSkills, repairingSkills),
                new Promise((resolve) => setTimeout(resolve, 250)),
            ]);
        } catch (err) {
            container.innerHTML = `<div class="skills-load-error">Failed to render skills: ${escapeHtml(err.message || err)}</div>`;
            console.warn('skills: render failed', err);
        } finally {
            refreshBtn.disabled = false;
            refreshBtn.classList.remove('is-loading');
            refreshBtn.textContent = originalText === 'Обновление...' ? 'Обновить' : originalText;
        }
    };

    refreshBtn.addEventListener('click', renderFn);
    attachActionHandlers(container, renderFn, reviewingSkills, repairingSkills, ctx);

    document.querySelectorAll('.skills-tab').forEach((btn) => {
        btn.addEventListener('click', () => {
            const tabName = btn.dataset.tab;
            activateTab(tabName);
            if (tabName === 'marketplace') {
                renderMarketplacePane().catch((err) => {
                    showToast(`ClawHub failed: ${err.message || err}`, 'danger');
                });
            } else if (tabName === 'ouroboroshub') {
                renderOuroborosHubPane().catch((err) => {
                    showToast(`OuroborosHub failed: ${err.message || err}`, 'danger');
                });
            }
        });
    });

    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'skills') {
            renderFn();
        }
    });
    renderFn();
}
