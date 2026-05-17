import { apiFetch } from './api_client.js';
/**
 * MCP (Model Context Protocol) client UI — manages the multi-server card
 * widget rendered inside Settings → Advanced.
 *
 * Responsibilities:
 *   - Render the list of configured MCP servers as cards (form fields).
 *   - Add / edit / remove / enable/disable individual entries.
 *   - Read masked auth tokens from /api/settings; keep them masked unless
 *     the user explicitly types a new value (so a Save round-trip doesn't
 *     overwrite the real token with the mask).
 *   - Probe a candidate via POST /api/mcp/test before saving.
 *   - Refresh the discovered tools per server via POST /api/mcp/refresh.
 *   - Show status / tool counts / errors via GET /api/mcp/status.
 *
 * The module is intentionally self-contained: settings.js wires it up
 * with `applyMcpSettings` (load) and `collectMcpSettings` (save).
 */
import { escapeHtmlAttr as escapeHtml } from './utils.js';

const TRANSPORTS = [
    { value: 'streamable_http', label: 'Streamable HTTP' },
    { value: 'sse', label: 'SSE (Server-Sent Events)' },
];

let mcpServers = [];
let mcpStatusByServer = {};
let mcpStatusEnvelope = null;
let mcpDirtyTokens = new Set();
let onChangeCallback = null;

function looksMasked(value) {
    const text = String(value ?? '').trim();
    if (!text) return false;
    return text === '***' || text.endsWith('...');
}

function emptyServer() {
    return {
        id: '',
        name: '',
        enabled: false,
        transport: 'streamable_http',
        url: '',
        auth_header: 'Authorization',
        auth_token: '',
        allowed_tools: [],
    };
}

function notifyChanged() {
    if (typeof onChangeCallback === 'function') {
        try { onChangeCallback(); } catch (err) { /* swallow */ }
    }
}

function renderServerCard(server, index) {
    const id = String(server.id ?? '');
    const name = String(server.name ?? '');
    const transport = String(server.transport ?? 'streamable_http');
    const url = String(server.url ?? '');
    const authHeader = String(server.auth_header ?? 'Authorization');
    const authToken = String(server.auth_token ?? '');
    const enabled = server.enabled === true || server.enabled === 'True' || server.enabled === 'true';
    const allowedTools = Array.isArray(server.allowed_tools) ? server.allowed_tools.join(', ') : '';
    const transportOptions = TRANSPORTS.map((opt) => {
        const selected = opt.value === transport ? ' selected' : '';
        return `<option value="${escapeHtml(opt.value)}"${selected}>${escapeHtml(opt.label)}</option>`;
    }).join('');
    const status = mcpStatusByServer[id] || null;
    const toolCount = status ? Number(status.tool_count || 0) : 0;
    const lastError = status ? String(status.last_error || '') : '';
    const lastRefreshed = status ? String(status.last_refreshed || '') : '';
    const tools = status && Array.isArray(status.tools) ? status.tools : [];

    let statusBadgeText = 'Ещё не обновлялось';
    let statusClass = 'mcp-server-status-muted';
    if (lastError) {
        statusBadgeText = `Ошибка: ${lastError}`;
        statusClass = 'mcp-server-status-danger';
    } else if (toolCount > 0) {
        statusBadgeText = `Найдено ${toolCount} инструмент${toolCount === 1 ? '' : (toolCount < 5 ? 'а' : 'ов')}`;
        statusClass = 'mcp-server-status-ok';
    } else if (lastRefreshed) {
        statusBadgeText = 'Инструменты не найдены';
        statusClass = 'mcp-server-status-warn';
    }

    const toolsHtml = tools.length
        ? `<ul class="mcp-tools-list">${tools.map((t) => `
                <li>
                    <strong>${escapeHtml(t.name || t.prefixed_name || '')}</strong>
                    ${t.description ? `<span class="mcp-tool-desc">${escapeHtml(String(t.description).slice(0, 220))}</span>` : ''}
                </li>
            `).join('')}</ul>`
        : '';

    const authPlaceholder = authToken && looksMasked(authToken)
        ? authToken
        : (authToken ? '••••••' : 'Bearer xxxxx (optional)');

    return `
        <article class="mcp-server-card" data-mcp-card data-mcp-index="${index}">
            <header class="mcp-server-card-head">
                <div class="mcp-server-card-title">
                    <strong>${escapeHtml(name || id || `MCP Server ${index + 1}`)}</strong>
                    <span class="mcp-server-status ${statusClass}">${escapeHtml(statusBadgeText)}</span>
                </div>
                <div class="mcp-server-card-actions">
                    <label class="mcp-server-enabled">
                        <input type="checkbox" data-mcp-field="enabled" ${enabled ? 'checked' : ''}>
                        <span>Включён</span>
                    </label>
                    <button type="button" class="settings-ghost-btn" data-mcp-test>Тест</button>
                    <button type="button" class="settings-ghost-btn" data-mcp-refresh>Обновить инструменты</button>
                    <button type="button" class="settings-ghost-btn mcp-server-remove" data-mcp-remove>Удалить</button>
                </div>
            </header>
            <div class="form-grid two">
                <div class="form-field">
                    <label>ID сервера</label>
                    <input type="text" data-mcp-field="id" value="${escapeHtml(id)}" placeholder="github" autocomplete="off" spellcheck="false">
                </div>
                <div class="form-field">
                    <label>Отображаемое имя</label>
                    <input type="text" data-mcp-field="name" value="${escapeHtml(name)}" placeholder="GitHub MCP" autocomplete="off" spellcheck="false">
                </div>
            </div>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Транспорт</label>
                    <select data-mcp-field="transport">${transportOptions}</select>
                </div>
                <div class="form-field">
                    <label>URL сервера</label>
                    <input type="text" data-mcp-field="url" value="${escapeHtml(url)}" placeholder="https://example.com/mcp" autocomplete="off" spellcheck="false">
                </div>
            </div>
            <div class="form-grid two">
                <div class="form-field">
                    <label>Заголовок авторизации</label>
                    <input type="text" data-mcp-field="auth_header" value="${escapeHtml(authHeader)}" placeholder="Authorization" autocomplete="off" spellcheck="false">
                </div>
                <div class="form-field">
                    <label>Токен авторизации (необязательно)</label>
                    <div class="secret-input-row">
                        <input type="password" data-mcp-field="auth_token" value="${escapeHtml(authToken)}" placeholder="${escapeHtml(authPlaceholder)}" autocomplete="off" spellcheck="false">
                        <button type="button" class="settings-ghost-btn" data-mcp-token-toggle>Показать</button>
                        <button type="button" class="settings-ghost-btn" data-mcp-token-clear>Очистить</button>
                    </div>
                </div>
            </div>
            <div class="form-row">
                <div class="form-field">
                    <label>Разрешённые инструменты (необязательно, через запятую)</label>
                    <input type="text" data-mcp-field="allowed_tools" value="${escapeHtml(allowedTools)}" placeholder="search, read_repo" autocomplete="off" spellcheck="false">
                </div>
            </div>
            <div class="settings-inline-status mcp-server-message" data-mcp-message hidden></div>
            ${toolsHtml ? `<details class="mcp-tools-disclosure"><summary>Найденные инструменты</summary>${toolsHtml}</details>` : ''}
        </article>
    `;
}

function bindCardEvents(card) {
    const idx = Number(card.dataset.mcpIndex || 0);
    const setMessage = (text, tone = 'muted') => {
        const el = card.querySelector('[data-mcp-message]');
        if (!el) return;
        if (!text) {
            el.hidden = true;
            el.textContent = '';
            return;
        }
        el.textContent = text;
        el.dataset.tone = tone;
        el.hidden = false;
    };

    card.querySelectorAll('[data-mcp-field]').forEach((input) => {
        input.addEventListener('input', () => {
            const field = input.dataset.mcpField;
            const server = mcpServers[idx];
            if (!server) return;
            if (field === 'enabled') {
                server.enabled = Boolean(input.checked);
            } else if (field === 'allowed_tools') {
                server.allowed_tools = String(input.value || '')
                    .split(',')
                    .map((s) => s.trim())
                    .filter(Boolean);
            } else if (field === 'auth_token') {
                if (looksMasked(input.value)) {
                    mcpDirtyTokens.delete(`${idx}`);
                } else {
                    mcpDirtyTokens.add(`${idx}`);
                }
                server.auth_token = input.value;
            } else {
                server[field] = input.value;
            }
            notifyChanged();
        });
        input.addEventListener('change', () => {
            const field = input.dataset.mcpField;
            const server = mcpServers[idx];
            if (!server) return;
            if (field === 'enabled') {
                server.enabled = Boolean(input.checked);
                notifyChanged();
            }
        });
    });

    const tokenInput = card.querySelector('[data-mcp-field="auth_token"]');
    const tokenToggle = card.querySelector('[data-mcp-token-toggle]');
    const tokenClear = card.querySelector('[data-mcp-token-clear]');
    if (tokenToggle && tokenInput) {
        tokenToggle.addEventListener('click', () => {
            if (tokenInput.type === 'password') {
                tokenInput.type = 'text';
                tokenToggle.textContent = 'Скрыть';
            } else {
                tokenInput.type = 'password';
                tokenToggle.textContent = 'Показать';
            }
        });
    }
    if (tokenClear && tokenInput) {
        tokenClear.addEventListener('click', () => {
            tokenInput.value = '';
            tokenInput.type = 'password';
            const server = mcpServers[idx];
            if (server) server.auth_token = '';
            mcpDirtyTokens.add(`${idx}`);
            notifyChanged();
        });
    }

    const removeBtn = card.querySelector('[data-mcp-remove]');
    if (removeBtn) {
        removeBtn.addEventListener('click', () => {
            mcpServers.splice(idx, 1);
            mcpDirtyTokens.delete(`${idx}`);
            renderAll();
            notifyChanged();
        });
    }

    const testBtn = card.querySelector('[data-mcp-test]');
    if (testBtn) {
        testBtn.addEventListener('click', async () => {
            const server = mcpServers[idx];
            if (!server) return;
            testBtn.disabled = true;
            setMessage('Проверка соединения...', 'muted');
            try {
                // When the auth_token is still masked AND the server has a
                // persisted id, send BOTH the edited candidate and server_id.
                // The backend rehydrates the real token from disk while
                // still testing the user's current URL/transport/header edits.
                // Without this, Test either probes without credentials or
                // ignores the edited candidate and checks stale saved config.
                const sid = String(server.id || '').trim();
                const tokenMasked = looksMasked(server.auth_token);
                let body;
                if (sid && tokenMasked) {
                    body = JSON.stringify({ server_id: sid, server: { ...server } });
                } else {
                    body = JSON.stringify({ server: serverForTest(server) });
                }
                const resp = await apiFetch('/api/mcp/test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body,
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok || data.ok === false) {
                    setMessage(`Тест не пройден: ${data.error || `HTTP ${resp.status}`}`, 'danger');
                } else {
                    const count = Number(data.tool_count || 0);
                    setMessage(`Тест пройден — найдено ${count} инструмент${count === 1 ? '' : (count < 5 ? 'а' : 'ов')}.`, 'ok');
                }
            } catch (err) {
                setMessage(`Тест не пройден: ${err && err.message ? err.message : err}`, 'danger');
            } finally {
                testBtn.disabled = false;
            }
        });
    }

    const refreshBtn = card.querySelector('[data-mcp-refresh]');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            const server = mcpServers[idx];
            if (!server) return;
            const sid = String(server.id || '').trim();
            if (!sid) {
                setMessage('Сохраните сервер (с ID) перед обновлением.', 'warn');
                return;
            }
            refreshBtn.disabled = true;
            setMessage('Обновление инструментов...', 'muted');
            try {
                const resp = await apiFetch('/api/mcp/refresh', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ server_id: sid }),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok || data.ok === false) {
                    setMessage(`Ошибка обновления: ${data.error || `HTTP ${resp.status}`}`, 'danger');
                } else {
                    const cnt = Number(data.tool_count || 0);
                    setMessage(`Обновлено — найдено ${cnt} инструмент${cnt === 1 ? '' : (cnt < 5 ? 'а' : 'ов')}.`, 'ok');
                    await refreshStatus();
                }
            } catch (err) {
                setMessage(`Ошибка обновления: ${err && err.message ? err.message : err}`, 'danger');
            } finally {
                refreshBtn.disabled = false;
            }
        });
    }
}

function serverForTest(server) {
    const out = { ...server };
    if (looksMasked(out.auth_token)) {
        // The backend ``/api/mcp/test`` accepts a ``server_id`` form when
        // the token is still masked; we prefer the inline form so the
        // user can tweak URL/transport before persisting. Drop the masked
        // value so the server treats this as "no auth" rather than
        // sending the literal mask string as a Bearer token.
        out.auth_token = '';
    }
    return out;
}

function renderAll() {
    const host = document.getElementById('mcp-servers-list');
    if (!host) return;
    if (!mcpServers.length) {
        host.innerHTML = '<div class="muted">MCP серверы не настроены. Нажмите «Добавить сервер», чтобы начать.</div>';
        return;
    }
    host.innerHTML = mcpServers.map((s, idx) => renderServerCard(s, idx)).join('');
    host.querySelectorAll('[data-mcp-card]').forEach((card) => bindCardEvents(card));
}

function renderEnvelopeStatus() {
    const el = document.getElementById('mcp-global-status');
    if (!el) return;
    if (!mcpStatusEnvelope) {
        el.textContent = '';
        el.dataset.tone = 'muted';
        return;
    }
    if (!mcpStatusEnvelope.sdk_available) {
        el.textContent = `MCP SDK не установлен: ${mcpStatusEnvelope.sdk_error || 'установите `mcp>=1.6` для включения MCP интеграции.'}`;
        el.dataset.tone = 'warn';
        return;
    }
    if (!mcpStatusEnvelope.enabled) {
        el.textContent = 'MCP клиент отключён. Включите выше для обнаружения инструментов.';
        el.dataset.tone = 'muted';
        return;
    }
    const total = Array.isArray(mcpStatusEnvelope.servers) ? mcpStatusEnvelope.servers.length : 0;
    const totalTools = Array.isArray(mcpStatusEnvelope.servers)
        ? mcpStatusEnvelope.servers.reduce((sum, s) => sum + Number(s.tool_count || 0), 0)
        : 0;
    el.textContent = `Настроено ${total} сервер${total === 1 ? '' : (total < 5 ? 'а' : 'ов')}, найдено ${totalTools} инструмент${totalTools === 1 ? '' : (totalTools < 5 ? 'а' : 'ов')}.`;
    el.dataset.tone = 'ok';
}

async function refreshStatus() {
    try {
        const resp = await apiFetch('/api/mcp/status', { cache: 'no-store' });
        if (!resp.ok) return;
        const data = await resp.json();
        mcpStatusEnvelope = data;
        const map = {};
        for (const entry of data.servers || []) {
            if (entry && entry.id) map[entry.id] = entry;
        }
        mcpStatusByServer = map;
        renderEnvelopeStatus();
        renderAll();
    } catch (err) {
        // Best-effort — don't break the Settings page.
    }
}

function bindAddButton() {
    const btn = document.getElementById('btn-mcp-add-server');
    if (!btn) return;
    btn.addEventListener('click', () => {
        mcpServers.push(emptyServer());
        renderAll();
        notifyChanged();
    });
}

function bindRefreshAllButton() {
    const btn = document.getElementById('btn-mcp-refresh-all');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        const wasText = btn.textContent;
        btn.textContent = 'Обновление...';
        try {
            await apiFetch('/api/mcp/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            await refreshStatus();
        } finally {
            btn.disabled = false;
            btn.textContent = wasText;
        }
    });
}

export function initMcpSettings({ onChange } = {}) {
    onChangeCallback = typeof onChange === 'function' ? onChange : null;
    bindAddButton();
    bindRefreshAllButton();
    const enabled = document.getElementById('s-mcp-enabled');
    if (enabled) enabled.addEventListener('change', notifyChanged);
    const timeout = document.getElementById('s-mcp-tool-timeout');
    if (timeout) timeout.addEventListener('input', notifyChanged);
}

export function applyMcpSettings(settings) {
    const enabledCheckbox = document.getElementById('s-mcp-enabled');
    if (enabledCheckbox) {
        enabledCheckbox.checked = settings.MCP_ENABLED === true || settings.MCP_ENABLED === 'True';
    }
    const timeoutInput = document.getElementById('s-mcp-tool-timeout');
    if (timeoutInput) {
        const value = Number(settings.MCP_TOOL_TIMEOUT_SEC || 60);
        timeoutInput.value = String(Number.isFinite(value) && value > 0 ? value : 60);
    }
    const incoming = Array.isArray(settings.MCP_SERVERS) ? settings.MCP_SERVERS : [];
    mcpServers = incoming.map((s) => ({
        id: String(s.id ?? ''),
        name: String(s.name ?? ''),
        enabled: Boolean(s.enabled),
        transport: String(s.transport ?? 'streamable_http'),
        url: String(s.url ?? ''),
        auth_header: String(s.auth_header ?? 'Authorization'),
        auth_token: String(s.auth_token ?? ''),
        allowed_tools: Array.isArray(s.allowed_tools) ? s.allowed_tools.map(String) : [],
    }));
    mcpDirtyTokens = new Set();
    renderAll();
    refreshStatus();
}

export function collectMcpSettings() {
    const enabledCheckbox = document.getElementById('s-mcp-enabled');
    const timeoutInput = document.getElementById('s-mcp-tool-timeout');
    const enabled = enabledCheckbox ? enabledCheckbox.checked : false;
    const timeoutRaw = timeoutInput ? Number(timeoutInput.value) : 60;
    const timeout = Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? Math.floor(timeoutRaw) : 60;
    const out = {
        MCP_ENABLED: Boolean(enabled),
        MCP_TOOL_TIMEOUT_SEC: timeout,
        MCP_SERVERS: mcpServers.map((s) => ({
            id: String(s.id || '').trim(),
            name: String(s.name || '').trim(),
            enabled: Boolean(s.enabled),
            transport: String(s.transport || 'streamable_http'),
            url: String(s.url || '').trim(),
            auth_header: String(s.auth_header || 'Authorization').trim() || 'Authorization',
            auth_token: String(s.auth_token || ''),
            allowed_tools: Array.isArray(s.allowed_tools) ? s.allowed_tools.map(String) : [],
        })),
    };
    return out;
}
