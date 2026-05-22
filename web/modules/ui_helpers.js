import { apiFetch } from './api_client.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';

const TONES = new Set(['ok', 'danger', 'warn', 'muted', 'info', 'error', 'success']);

export function normalizeTone(tone = 'muted', fallback = 'muted') {
    const clean = String(tone || fallback).toLowerCase();
    return clean === 'error' ? 'danger' : clean === 'success' ? 'ok' : TONES.has(clean) ? clean : fallback;
}

export function renderToneBadge(label, tone = 'muted', className = 'skills-badge') {
    const cleanTone = normalizeTone(tone);
    return `<span class="${className} ${className}-${cleanTone}">${escapeHtml(label || '')}</span>`;
}

export function installedTime(item) {
    const time = Date.parse(item?.installed_at || item?.provenance?.installed_at || item?.provenance?.updated_at || '');
    return Number.isFinite(time) ? time : 0;
}

export function formatRelativeAge(time, freshLabel = 'Just installed') {
    if (!time) return '';
    const minutes = Math.floor(Math.max(0, Date.now() - time) / 60000);
    if (minutes < 2) return freshLabel;
    if (minutes < 90) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return days < 45 ? `${days}d ago` : new Date(time).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export function setInlineStatus(el, text, tone = 'muted') {
    if (el) { el.textContent = text || ''; el.dataset.tone = normalizeTone(tone); }
}

export async function downloadViaHostBridge(url, filename = 'download', { openExternal = false, fetchOptions = {} } = {}) {
    const bridge = window.pywebview?.api?.download_file_to_downloads;
    if (bridge) {
        const result = await bridge(url, filename, Boolean(openExternal));
        if (!result?.ok) throw new Error(result?.error || 'desktop download failed');
        return { ...result, native: true };
    }
    const resp = await apiFetch(url, fetchOptions);
    if (!resp.ok) throw new Error(`download failed: HTTP ${resp.status}`);
    const blobUrl = URL.createObjectURL(await resp.blob());
    const link = document.createElement('a');
    Object.assign(link, { href: blobUrl, download: filename, rel: 'noopener' });
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    return { ok: true, native: false };
}
