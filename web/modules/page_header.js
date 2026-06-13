import { escapeHtmlAttr as escapeHtml } from './utils.js';

function classAttr(parts) {
    return parts.filter(Boolean).join(' ');
}

export function renderPageHeader({
    title,
    icon = '',
    description = '',
    actionsHtml = '',
    tabsHtml = '',
    variant = '',
    className = '',
} = {}) {
    const variantClass = variant ? `app-page-header-${escapeHtml(variant)}` : '';
    const iconHtml = icon ? `<span class="app-page-icon" aria-hidden="true">${icon}</span>` : '';
    const descriptionHtml = description
        ? `<p class="app-page-description">${escapeHtml(description)}</p>`
        : '';
    const actions = actionsHtml
        ? `<div class="app-page-actions">${actionsHtml}</div>`
        : '';
    const tabs = tabsHtml
        ? `<div class="app-page-tabs">${tabsHtml}</div>`
        : '';
    return `
        <div class="${classAttr(['page-header', 'app-page-header', variantClass, className])}">
            <div class="app-page-title-block">
                <div class="app-page-title-row">
                    ${iconHtml}
                    <h2 class="app-page-title">${escapeHtml(title)}</h2>
                </div>
                ${descriptionHtml}
            </div>
            ${actions}
            ${tabs}
        </div>
    `;
}

export function renderTabStrip({
    items = [],
    active = '',
    dataAttr,
    activeClass = 'active',
    ariaLabel = 'Просмотры страницы',
    stripClass = '',
    tabClass = '',
} = {}) {
    const attr = String(dataAttr || '').trim();
    if (!attr) {
        throw new Error('renderTabStrip requires dataAttr');
    }
    const buttons = items.map((item) => {
        const value = String(item.value ?? item.id ?? '');
        const isActive = value === active;
        const pill = item.pillId
            ? `<span class="${classAttr(['app-tab-pill', item.pillClass || ''])}" id="${escapeHtml(item.pillId)}" hidden></span>`
            : '';
        return `
            <button
                type="button"
                class="${classAttr(['app-tab', tabClass, item.className || '', isActive ? activeClass : ''])}"
                ${attr}="${escapeHtml(value)}"
                role="tab"
                aria-selected="${isActive ? 'true' : 'false'}"
            >
                ${escapeHtml(item.label ?? value)}
                ${pill}
            </button>
        `;
    }).join('');
    return `
        <div class="${classAttr(['app-tab-strip', stripClass])}" role="tablist" aria-label="${escapeHtml(ariaLabel)}">
            ${buttons}
        </div>
    `;
}
