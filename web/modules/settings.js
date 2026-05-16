import { refreshModelCatalog } from './settings_catalog.js';
import { bindEffortSegments, syncEffortSegments } from './settings_controls.js';
import { bindLocalModelControls } from './settings_local_model.js';
import { applyMcpSettings, collectMcpSettings, initMcpSettings } from './mcp_settings.js';
import { SECRET_KEYS, bindSecretInputs, bindSettingsTabs, renderSettingsPage } from './settings_ui.js';
import { showToast } from './toast.js';
import { escapeHtmlAttr as escapeHtml, formatDualVersion } from './utils.js';
import { apiFetch } from './api_client.js';

let markSettingsDirty = () => {};
const BASE_SECRET_KEYS = new Set(SECRET_KEYS.map(([key]) => key));

function byId(id) {
    return document.getElementById(id);
}

function applyInputValue(id, value) {
    byId(id).value = value === undefined || value === null ? '' : value;
}

function applyCheckboxValue(id, value) {
    byId(id).checked = isTruthySetting(value);
}

function isTruthySetting(value) {
    const normalized = String(value ?? '').trim().toLowerCase();
    return value === true || ['true', '1', 'yes', 'on'].includes(normalized);
}

function setStatus(text, tone = 'ok') {
    const status = byId('settings-status');
    status.textContent = text;
    status.dataset.tone = tone;
}

function readInt(id, fallback) {
    const value = parseInt(byId(id).value, 10);
    return Number.isNaN(value) ? fallback : value;
}

function readFloat(id, fallback) {
    const value = parseFloat(byId(id).value);
    return Number.isNaN(value) ? fallback : value;
}

function resetSecretClearFlags(root) {
    root.querySelectorAll('.secret-input').forEach((input) => {
        delete input.dataset.forceClear;
        input.type = 'password';
    });
    root.querySelectorAll('.secret-toggle').forEach((button) => {
        button.textContent = 'Show';
    });
}

function applySecretInputs(root, settings) {
    root.querySelectorAll('[data-secret-setting]').forEach((input) => {
        applyInputValue(input.id, settings[input.dataset.secretSetting]);
    });
}


function wireSecretRow(row) {
    const input = row.querySelector('.secret-input');
    const toggle = row.querySelector('[data-row-secret-toggle]');
    const clear = row.querySelector('[data-row-secret-clear]');
    if (input) input.addEventListener('input', () => { if (input.value.trim()) delete input.dataset.forceClear; });
    if (toggle && input) toggle.addEventListener('click', () => { input.type = input.type === 'password' ? 'text' : 'password'; toggle.textContent = input.type === 'password' ? 'Show' : 'Hide'; });
    if (clear && input) clear.addEventListener('click', () => { input.value = ''; input.type = 'password'; input.dataset.forceClear = '1'; if (toggle) toggle.textContent = 'Show'; markSettingsDirty(); });
}

function customSecretRow(key = '', value = '') {
    const id = `custom-secret-${Math.random().toString(36).slice(2)}`;
    const row = document.createElement('div');
    row.className = 'settings-custom-secret-row';
    row.dataset.customSecretRow = '1';
    row.innerHTML = `
        <div class="form-field settings-custom-secret-key"><label>Key</label><input data-custom-secret-key value="${escapeHtml(key)}" placeholder="SLACK_WEBHOOK_URL" spellcheck="false"></div>
        <div class="form-field settings-custom-secret-value"><label>Value</label><div class="secret-input-row">
            <input id="${id}" data-custom-secret-value class="secret-input" type="password" value="${escapeHtml(value || '')}" placeholder="Secret value">
            <button type="button" class="settings-ghost-btn" data-row-secret-toggle>Show</button>
            <button type="button" class="settings-ghost-btn" data-row-secret-clear>Clear</button>
        </div><div class="settings-inline-note" data-custom-secret-error hidden></div></div>
        <button type="button" class="settings-ghost-btn settings-custom-secret-remove" data-custom-secret-remove>Remove</button>`;
    wireSecretRow(row);
    row.querySelector('[data-custom-secret-remove]')?.addEventListener('click', () => { row.dataset.removeCustomSecret = '1'; row.hidden = true; markSettingsDirty(); });
    return row;
}

function renderCustomSecrets(root, settings) {
    const host = root.querySelector('#custom-secrets-list');
    if (!host) return;
    host.innerHTML = '';
    const keys = Array.isArray(settings?._meta?.custom_secret_keys) ? settings._meta.custom_secret_keys : [];
    keys.forEach((key) => host.appendChild(customSecretRow(key, settings[key] || '')));
    if (!keys.length) host.innerHTML = '<div class="muted">No custom keys yet.</div>';
}

function renderRequestedSkillSecrets(root, skills, settings) {
    const host = root.querySelector('#skill-requested-secrets');
    if (!host) return;
    const keys = [];
    (Array.isArray(skills) ? skills : []).forEach((skill) => {
        (skill?.grants?.requested_keys || []).forEach((key) => {
            const normalized = String(key || '').trim();
            if (normalized && !BASE_SECRET_KEYS.has(normalized)) keys.push(normalized);
        });
    });
    const unique = Array.from(new Set(keys)).sort((a, b) => a.localeCompare(b));
    if (!unique.length) { host.innerHTML = '<div class="muted">No skill-requested secrets.</div>'; return; }
    host.innerHTML = '';
    unique.forEach((key, idx) => {
        const id = `requested-secret-${idx}`;
        const el = document.createElement('div');
        el.className = 'settings-requested-secret-row';
        el.innerHTML = `<div class="form-field"><label>${escapeHtml(key)}</label><div class="secret-input-row">
            <input id="${id}" data-secret-setting="${escapeHtml(key)}" class="secret-input" type="password" value="${escapeHtml(settings[key] || '')}" placeholder="Secret value">
            <button type="button" class="settings-ghost-btn" data-row-secret-toggle>Show</button>
            <button type="button" class="settings-ghost-btn" data-row-secret-clear>Clear</button>
        </div></div>`;
        wireSecretRow(el); host.appendChild(el);
    });
}

function renderExtensionSettingsSections(root, sections) {
    const host = root.querySelector('#extension-settings-sections');
    if (!host) return;
    const items = Array.isArray(sections) ? sections : [];
    if (!items.length) {
        host.innerHTML = '<div class="muted">No extension settings registered.</div>';
        return;
    }
    const cleanExtensionRoute = (value) => {
        const route = String(value || '').trim().replace(/^\/+/, '');
        const parts = route.split('/').filter(Boolean);
        if (!route || route.includes('\\') || parts.some((part) => part === '.' || part === '..')) {
            return '';
        }
        return parts.map(encodeURIComponent).join('/');
    };
    const fieldHtml = (field) => {
        const name = escapeHtml(field.name || '');
        const label = escapeHtml(field.label || field.name || '');
        const placeholder = escapeHtml(field.placeholder || '');
        const type = String(field.type || 'text');
        if (type === 'textarea') {
            return `<label class="form-field"><span>${label}</span><textarea name="${name}" placeholder="${placeholder}"></textarea></label>`;
        }
        if (type === 'checkbox') {
            return `<label class="settings-extension-checkbox"><input type="checkbox" name="${name}"><span>${label}</span></label>`;
        }
        return `<label class="form-field"><span>${label}</span><input name="${name}" type="${escapeHtml(type)}" placeholder="${placeholder}"></label>`;
    };
    const componentHtml = (section, component, idx) => {
        const type = String(component.type || '');
        if (type === 'markdown') {
            return `<div class="settings-section-copy">${escapeHtml(component.text || '')}</div>`;
        }
        if (type === 'json') {
            return `<details class="widget-json"><summary>${escapeHtml(component.label || 'JSON')}</summary><pre>${escapeHtml(JSON.stringify(component.value || component.data || {}, null, 2))}</pre></details>`;
        }
        if (type === 'form' || type === 'action') {
            const fields = Array.isArray(component.fields) ? component.fields : [];
            const route = cleanExtensionRoute(component.route || component.api_route || '');
            if (!route) {
                return '<div class="settings-inline-note">Invalid extension settings route.</div>';
            }
            return `
                <form class="settings-extension-form" data-extension-settings-form data-skill="${escapeHtml(section.skill || '')}" data-route="${escapeHtml(route)}">
                    <div class="form-grid two">${fields.map(fieldHtml).join('')}</div>
                    <button class="btn btn-primary btn-sm" type="submit">${escapeHtml(component.submit_label || component.label || 'Save')}</button>
                    <div class="settings-inline-status" data-extension-settings-status></div>
                </form>
            `;
        }
        return `<div class="settings-inline-note">Unsupported extension settings component ${idx + 1}: ${escapeHtml(type || 'unknown')}</div>`;
    };
    host.innerHTML = items.map((section) => {
        const title = escapeHtml(section.title || section.section_id || section.key || 'Extension settings');
        const skill = escapeHtml(section.skill || '');
        const components = Array.isArray(section.render?.components) ? section.render.components : [];
        return `
            <article class="settings-extension-section">
                <div class="settings-extension-section-head">
                    <strong>${title}</strong>
                    ${skill ? `<span class="settings-inline-note">from ${skill}</span>` : ''}
                </div>
                <div class="settings-extension-components">
                    ${components.length ? components.map((component, idx) => componentHtml(section, component, idx)).join('') : '<div class="muted">No declarative components.</div>'}
                </div>
            </article>
        `;
    }).join('');
    host.querySelectorAll('[data-extension-settings-form]').forEach((form) => {
        form.addEventListener('submit', async (event) => {
            event.preventDefault();
            const status = form.querySelector('[data-extension-settings-status]');
            const skill = form.dataset.skill || '';
            const route = form.dataset.route || '';
            if (!skill || !route) return;
            const values = {};
            new FormData(form).forEach((value, key) => { values[key] = value; });
            form.querySelectorAll('input[type="checkbox"]').forEach((input) => {
                values[input.name] = input.checked;
            });
            if (status) {
                status.textContent = 'Saving...';
                status.dataset.tone = 'muted';
            }
            try {
                const cleanRoute = cleanExtensionRoute(route);
                if (!cleanRoute) throw new Error('invalid extension settings route');
                const resp = await apiFetch(`/api/extensions/${encodeURIComponent(skill)}/${cleanRoute}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(values),
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                if (status) {
                    status.textContent = data.message || 'Saved.';
                    status.dataset.tone = 'ok';
                }
            } catch (err) {
                if (status) {
                    status.textContent = err.message || String(err);
                    status.dataset.tone = 'danger';
                }
            }
        });
    });
}

function collectSecretValue(id, body) {
    const input = byId(id);
    if (!input) return;
    const settingKey = input.dataset.secretSetting;
    if (!settingKey) return;
    if (input.dataset.forceClear === '1') {
        body[settingKey] = '';
        return;
    }
    const value = input.value;
    if (value && !value.includes('...')) body[settingKey] = value;
}

// Suggestion pills for the model picker.  Should include every default
// shipped in ``ouroboros/config.py::SETTINGS_DEFAULTS`` for the four
// model lanes (main / code / light / fallback) so a future config bump
// does not silently strand the UI on a stale id. Extra direct-provider
// variants (``openai::gpt-5.5``, etc.) stay as useful pills even when
// they are not the OpenRouter default.
const SETTINGS_FALLBACK_MODELS = [
    'anthropic::claude-opus-4-6',
    'anthropic::claude-sonnet-4-6',
    'openai::gpt-5.5',
    'openai::gpt-5.5-mini',
    'openai/gpt-5.5',
    'anthropic/claude-opus-4.6',
    'anthropic/claude-sonnet-4.6',
    'google/gemini-3.1-pro-preview',
];

let settingsModelCatalogItems = SETTINGS_FALLBACK_MODELS.map((value) => ({ value, label: 'Suggested model' }));

export function initSettings({ state, setBeforePageLeave, ws } = {}) {
    const page = document.createElement('div');
    page.id = 'page-settings';
    page.className = 'page app-page-glass';
    page.innerHTML = renderSettingsPage();
    document.getElementById('content').appendChild(page);

    const activateSettingsTab = (tabName) => {
        if (typeof page.activateSettingsTab === 'function') {
            page.activateSettingsTab(tabName);
        }
    };
    bindSettingsTabs(page, { state });
    bindSecretInputs(page);
    bindEffortSegments(page);
    bindLocalModelControls({ state });
    // Populate the About sub-tab version label from /api/health so the
    // existing #nav-version short label and the in-Settings detailed version
    // string stay consistent. The fetch is best-effort — if it fails the
    // label simply remains empty rather than blocking settings load.
    apiFetch('/api/health')
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d) => {
            const verEl = document.getElementById('about-version');
            if (verEl) verEl.textContent = formatDualVersion(d);
        })
        .catch(() => { /* about version is best-effort */ });
    let currentSettings = {};
    let claudeCodePollStarted = false;
    let extensionRefreshPending = false;
    // v4.33.1 status_label priority fix: even when the user has not configured
    // ANTHROPIC_API_KEY, we still surface the runtime card when the backend
    // reports status="error" (e.g. SDK below baseline). Otherwise a version-gate
    // failure is silently hidden until the user adds a key, which defeats the
    // whole point of prioritizing error over no_api_key in `status_label`.
    let claudeRuntimeHasError = false;
    let settingsLoaded = false;
    let settingsBaseline = '';
    let settingsDirty = false;
    initMcpSettings({ onChange: updateSettingsDirtyState });

    function anthropicKeyConfigured() {
        const input = byId('s-anthropic');
        if (!input) return Boolean(String(currentSettings.ANTHROPIC_API_KEY || '').trim());
        if (input.dataset.forceClear === '1') return false;
        const liveValue = String(input.value || '').trim();
        if (liveValue) return true;
        return Boolean(String(currentSettings.ANTHROPIC_API_KEY || '').trim());
    }

    function shouldShowClaudeRuntimeCard() {
        // Show when the user has configured an Anthropic key, OR when the
        // backend has reported a concrete runtime error that the user needs
        // to see and repair (e.g. SDK below baseline, bundled CLI missing).
        return anthropicKeyConfigured() || claudeRuntimeHasError;
    }

    function renderClaudeCodeUi() {
        const panel = byId('settings-claude-code-panel');
        const note = byId('settings-claude-code-copy');
        const button = byId('btn-claude-code-install');
        const visible = shouldShowClaudeRuntimeCard();
        if (panel) panel.hidden = !visible;
        if (note) note.hidden = !visible;
        if (!visible) return;
        if (button && button.dataset.busy !== '1' && button.dataset.ready !== '1') {
            button.disabled = false;
            button.textContent = 'Repair Runtime';
        }
    }

    function syncSettingsLoadState() {
        const saveBtn = byId('btn-save-settings');
        if (saveBtn) {
            saveBtn.disabled = !settingsLoaded;
            saveBtn.title = settingsLoaded
                ? ''
                : 'Reload current settings successfully before saving.';
        }
    }

    function syncRuntimeModeBridgeState() {
        const hasBridge = Boolean(window.pywebview?.api?.request_runtime_mode_change);
        const group = document.querySelector('[data-runtime-mode-group]');
        if (group) {
            group.title = hasBridge
                ? 'Runtime mode changes require native launcher confirmation and restart.'
                : 'Runtime mode is view-only here. Use the desktop app or edit settings.json while Ouroboros is stopped.';
        }
        document.querySelectorAll('[data-runtime-mode-group] [data-effort-value]').forEach((button) => {
            button.disabled = !hasBridge;
        });
    }

    function snapshotSettingsDraft() {
        return JSON.stringify({
            ...collectBody(),
            OUROBOROS_RUNTIME_MODE_DRAFT: byId('s-runtime-mode')?.value || 'advanced',
        });
    }

    function setSettingsCleanBaseline() {
        settingsBaseline = snapshotSettingsDraft();
        settingsDirty = false;
        const indicator = byId('settings-unsaved-indicator');
        if (indicator) indicator.classList.remove('is-visible');
    }

    function updateSettingsDirtyState() {
        if (!settingsLoaded || !settingsBaseline) return;
        const nextDirty = snapshotSettingsDraft() !== settingsBaseline;
        if (nextDirty === settingsDirty) return;
        settingsDirty = nextDirty;
        const indicator = byId('settings-unsaved-indicator');
        if (indicator) indicator.classList.toggle('is-visible', settingsDirty);
    }

    function discardUnsavedSettingsDraft() {
        closeSettingsModelPickers();
        applySettings(currentSettings || {});
        setSettingsCleanBaseline();
        setStatus('', 'ok');
    }

    function applyClaudeCodeStatus(payload = {}) {
        const button = byId('btn-claude-code-install');
        const status = byId('settings-claude-code-status');
        const ready = Boolean(payload.ready);
        const installed = Boolean(payload.installed);
        const busy = Boolean(payload.busy);
        const error = String(payload.error || '').trim();
        // Track backend error state so `shouldShowClaudeRuntimeCard` can
        // surface the card even without a configured API key.
        claudeRuntimeHasError = Boolean(error);
        const message = String(payload.message || '').trim()
            || (ready ? 'Claude runtime ready.' : (installed ? 'Claude runtime available but not ready.' : 'Claude runtime not available.'));
        const tone = ready ? 'ok' : (error ? 'error' : (installed ? 'muted' : 'error'));
        if (status) {
            status.textContent = message;
            status.dataset.tone = tone;
        }
        if (button) {
            button.dataset.busy = busy ? '1' : '0';
            button.dataset.ready = ready ? '1' : '0';
            button.dataset.installed = installed ? '1' : '0';
            button.disabled = busy;
            button.textContent = busy ? 'Repairing...' : (ready ? 'Runtime OK' : 'Repair Runtime');
        }
        renderClaudeCodeUi();
    }

    async function refreshClaudeCodeStatus() {
        // Always poll the backend — status errors (e.g. SDK below baseline) must
        // surface even without a configured API key. The backend distinguishes
        // "no_api_key" from "error" via the v4.33.1 `status_label` priority fix.
        try {
            const resp = await apiFetch('/api/claude-code/status', { cache: 'no-store' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            applyClaudeCodeStatus(data);
        } catch (error) {
            applyClaudeCodeStatus({
                installed: false,
                ready: false,
                busy: false,
                error: String(error?.message || error || ''),
                message: `Claude runtime status check failed: ${String(error?.message || error || '')}`,
            });
        }
    }

    function syncAutoGrantBridgeState() {
        const hasBridge = Boolean(window.pywebview?.api?.request_auto_grant_reviewed_skills_change);
        const checkbox = byId('s-auto-grant-reviewed-skills');
        const label = checkbox?.closest('.local-toggle');
        if (checkbox) checkbox.disabled = !hasBridge;
        if (label) {
            label.title = hasBridge
                ? 'Requires native confirmation. Applies only after a fresh skill review pass and only to manifest-declared grants for that exact content hash.'
                : 'Reviewed-skill auto-grant requires the desktop launcher confirmation bridge. Stop Ouroboros and edit settings.json manually outside desktop mode.';
        }
    }

    function startClaudeCodePolling() {
        if (claudeCodePollStarted) return;
        claudeCodePollStarted = true;
        refreshClaudeCodeStatus();
        setInterval(() => {
            // Poll unconditionally so a below-baseline SDK stays visible even
            // after the user clears the Anthropic key.
            refreshClaudeCodeStatus();
        }, 3000);
    }

    function applySettings(s) {
        applyInputValue('s-openrouter', s.OPENROUTER_API_KEY);
        applyInputValue('s-openai', s.OPENAI_API_KEY);
        applyInputValue('s-openai-base-url', s.OPENAI_BASE_URL);
        applyInputValue('s-openai-compatible-key', s.OPENAI_COMPATIBLE_API_KEY);
        applyInputValue('s-openai-compatible-base-url', s.OPENAI_COMPATIBLE_BASE_URL);
        applyInputValue('s-cloudru-key', s.CLOUDRU_FOUNDATION_MODELS_API_KEY);
        applyInputValue('s-cloudru-base-url', s.CLOUDRU_FOUNDATION_MODELS_BASE_URL);
        applyInputValue('s-anthropic', s.ANTHROPIC_API_KEY);
        applyInputValue('s-network-password', s.OUROBOROS_NETWORK_PASSWORD);
        applyInputValue('s-server-host', s.OUROBOROS_SERVER_HOST || '127.0.0.1');

        applyInputValue('s-model', s.OUROBOROS_MODEL);
        applyInputValue('s-model-code', s.OUROBOROS_MODEL_CODE);
        applyInputValue('s-model-light', s.OUROBOROS_MODEL_LIGHT);
        applyInputValue('s-model-fallback', s.OUROBOROS_MODEL_FALLBACK);
        applyInputValue('s-claude-code-model', s.CLAUDE_CODE_MODEL);
        byId('s-effort-task').value = s.OUROBOROS_EFFORT_TASK || 'medium';
        byId('s-effort-evolution').value = s.OUROBOROS_EFFORT_EVOLUTION || 'high';
        byId('s-effort-review').value = s.OUROBOROS_EFFORT_REVIEW || 'medium';
        byId('s-effort-consciousness').value = s.OUROBOROS_EFFORT_CONSCIOUSNESS || 'low';
        applyInputValue('s-review-models', s.OUROBOROS_REVIEW_MODELS);
        applyInputValue('s-scope-review-model', s.OUROBOROS_SCOPE_REVIEW_MODEL);
        byId('s-effort-scope-review').value = s.OUROBOROS_EFFORT_SCOPE_REVIEW || 'high';
        byId('s-review-enforcement').value = s.OUROBOROS_REVIEW_ENFORCEMENT || 'advisory';
        byId('s-runtime-mode').value = s.OUROBOROS_RUNTIME_MODE || 'advanced';
        applyCheckboxValue('s-auto-grant-reviewed-skills', s.OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS);
        applyInputValue('s-skills-repo-path', s.OUROBOROS_SKILLS_REPO_PATH);
        applyInputValue('s-clawhub-registry-url', s.OUROBOROS_CLAWHUB_REGISTRY_URL);
        if (s.OUROBOROS_MAX_WORKERS) byId('s-workers').value = s.OUROBOROS_MAX_WORKERS;
        if (s.OUROBOROS_SOFT_TIMEOUT_SEC) byId('s-soft-timeout').value = s.OUROBOROS_SOFT_TIMEOUT_SEC;
        if (s.OUROBOROS_HARD_TIMEOUT_SEC) byId('s-hard-timeout').value = s.OUROBOROS_HARD_TIMEOUT_SEC;
        if (s.OUROBOROS_TOOL_TIMEOUT_SEC) byId('s-tool-timeout').value = s.OUROBOROS_TOOL_TIMEOUT_SEC;
        applyInputValue('s-websearch-model', s.OUROBOROS_WEBSEARCH_MODEL);
        applyInputValue('s-gh-repo', s.GITHUB_REPO);
        applySecretInputs(page, s);
        applyInputValue('s-local-source', s.LOCAL_MODEL_SOURCE);
        applyInputValue('s-local-filename', s.LOCAL_MODEL_FILENAME);
        if (s.LOCAL_MODEL_PORT) byId('s-local-port').value = s.LOCAL_MODEL_PORT;
        if (s.LOCAL_MODEL_N_GPU_LAYERS !== null && s.LOCAL_MODEL_N_GPU_LAYERS !== undefined) byId('s-local-gpu-layers').value = s.LOCAL_MODEL_N_GPU_LAYERS;
        if (s.LOCAL_MODEL_CONTEXT_LENGTH) byId('s-local-ctx').value = s.LOCAL_MODEL_CONTEXT_LENGTH;
        applyInputValue('s-local-chat-format', s.LOCAL_MODEL_CHAT_FORMAT);
        applyCheckboxValue('s-local-main', s.USE_LOCAL_MAIN);
        applyCheckboxValue('s-local-code', s.USE_LOCAL_CODE);
        applyCheckboxValue('s-local-light', s.USE_LOCAL_LIGHT);
        applyCheckboxValue('s-local-fallback', s.USE_LOCAL_FALLBACK);
        applyMcpSettings(s);
        resetSecretClearFlags(page);
        syncEffortSegments(page);
        syncRuntimeModeBridgeState();
    }

    function _renderNetworkHint(meta) {
        const hint = document.getElementById('settings-lan-hint');
        if (!hint || !meta) return;
        if (meta.reachability === 'loopback_only') {
            hint.innerHTML = 'Bound to <code>localhost</code>: only accessible from this machine. Set Server Bind Host to <code>0.0.0.0</code>, save, and restart for LAN access.';
            hint.dataset.tone = 'info';
            hint.hidden = false;
        } else if (meta.reachability === 'lan_reachable') {
            const url = escapeHtml(meta.recommended_url || '');
            const warning = escapeHtml(meta.warning || '');
            hint.innerHTML = `LAN URL: <a href="${url}" target="_blank" rel="noopener">${url}</a>${warning ? ' — <strong>' + warning + '</strong>' : ''}`;
            hint.dataset.tone = meta.warning ? 'warn' : 'ok';
            hint.hidden = false;
        } else if (meta.reachability === 'host_ip_unknown') {
            const url = escapeHtml(meta.recommended_url || '');
            const warning = escapeHtml(meta.warning || '');
            hint.innerHTML = `Server is listening on non-localhost but LAN IP could not be detected automatically. Try <code>${url}</code>.${warning ? ' <strong>' + warning + '</strong>' : ''}`;
            hint.dataset.tone = 'warn';
            hint.hidden = false;
        } else {
            hint.hidden = true;
        }
    }

    async function loadSettings() {
        const [settingsResp, extResp] = await Promise.all([
            apiFetch('/api/settings', { cache: 'no-store' }),
            apiFetch('/api/extensions', { cache: 'no-store' }).catch(() => null),
        ]);
        const data = await settingsResp.json().catch(() => ({}));
        const extData = extResp && extResp.ok ? await extResp.json().catch(() => ({})) : {};
        const sections = Array.isArray(extData?.live?.settings_sections)
            ? extData.live.settings_sections
            : [];
        if (!settingsResp.ok) throw new Error(data.error || `HTTP ${settingsResp.status}`);
        currentSettings = data;
        applySettings(data);
        renderExtensionSettingsSections(page, sections);
        renderRequestedSkillSecrets(page, extData.skills || [], data);
        renderCustomSecrets(page, data);
        setSettingsCleanBaseline();
        closeSettingsModelPickers();
        _renderNetworkHint(data._meta);
        renderClaudeCodeUi();
        settingsLoaded = true;
        markSettingsDirty = updateSettingsDirtyState;
    syncSettingsLoadState();
        // Always start polling so a below-baseline SDK surfaces even before
        // the user sets ANTHROPIC_API_KEY. `refreshClaudeCodeStatus` is now
        // unconditional, and `shouldShowClaudeRuntimeCard` uses the runtime
        // error signal to decide visibility.
        startClaudeCodePolling();
    }

    async function reloadSettingsWithFeedback() {
        setStatus('Loading settings...', 'muted');
        settingsLoaded = false;
        syncSettingsLoadState();
        try {
            await loadSettings();
            try {
                await refreshModelCatalog();
                setStatus('Settings loaded', 'ok');
            } catch (error) {
                setStatus(
                    `Settings loaded. Model catalog refresh failed: ${error.message || error}`,
                    'warn'
                );
            }
        } catch (error) {
            settingsLoaded = false;
            syncSettingsLoadState();
            setStatus(
                `Failed to load current settings. Save is disabled until reload succeeds: ${error.message || error}`,
                'warn'
            );
        }
    }

    async function refreshSettingsAfterExtensionChange(reason = 'skills changed') {
        if (extensionRefreshPending) return;
        if (settingsDirty) {
            setStatus(`Settings changed externally (${reason}). Reload after saving or discarding your draft.`, 'warn');
            return;
        }
        extensionRefreshPending = true;
        try {
            await loadSettings();
            setStatus('Settings refreshed', 'ok');
        } catch (error) {
            setStatus(`Settings refresh failed: ${error.message || error}`, 'warn');
        } finally {
            extensionRefreshPending = false;
        }
    }

    function collectBody() {
        const body = {
            OUROBOROS_MODEL: byId('s-model').value,
            OUROBOROS_MODEL_CODE: byId('s-model-code').value,
            OUROBOROS_MODEL_LIGHT: byId('s-model-light').value,
            OUROBOROS_MODEL_FALLBACK: byId('s-model-fallback').value,
            CLAUDE_CODE_MODEL: byId('s-claude-code-model').value || 'claude-opus-4-6[1m]',
            OUROBOROS_SERVER_HOST: (byId('s-server-host')?.value || '127.0.0.1').trim() || '127.0.0.1',
            OUROBOROS_EFFORT_TASK: byId('s-effort-task').value,
            OUROBOROS_EFFORT_EVOLUTION: byId('s-effort-evolution').value,
            OUROBOROS_EFFORT_REVIEW: byId('s-effort-review').value,
            OUROBOROS_EFFORT_CONSCIOUSNESS: byId('s-effort-consciousness').value,
            OUROBOROS_REVIEW_MODELS: byId('s-review-models').value.trim(),
            OUROBOROS_SCOPE_REVIEW_MODEL: byId('s-scope-review-model').value.trim(),
            OUROBOROS_EFFORT_SCOPE_REVIEW: byId('s-effort-scope-review').value,
            OUROBOROS_REVIEW_ENFORCEMENT: byId('s-review-enforcement').value,
            OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS: byId('s-auto-grant-reviewed-skills')?.checked ? 'true' : 'false',
            // OUROBOROS_RUNTIME_MODE is owner-only: /api/settings still
            // ignores it, while desktop mode changes go through the
            // launcher-native confirmation bridge after normal settings save.
            OUROBOROS_SKILLS_REPO_PATH: byId('s-skills-repo-path').value.trim(),
            OUROBOROS_CLAWHUB_REGISTRY_URL: byId('s-clawhub-registry-url')?.value.trim() || '',
            OUROBOROS_MAX_WORKERS: readInt('s-workers', 5),
            OUROBOROS_SOFT_TIMEOUT_SEC: readInt('s-soft-timeout', 600),
            OUROBOROS_HARD_TIMEOUT_SEC: readInt('s-hard-timeout', 1800),
            OUROBOROS_TOOL_TIMEOUT_SEC: readInt('s-tool-timeout', 120),
            OUROBOROS_WEBSEARCH_MODEL: byId('s-websearch-model').value.trim(),
            GITHUB_REPO: byId('s-gh-repo').value,
            LOCAL_MODEL_SOURCE: byId('s-local-source').value,
            LOCAL_MODEL_FILENAME: byId('s-local-filename').value,
            LOCAL_MODEL_PORT: readInt('s-local-port', 8766),
            LOCAL_MODEL_N_GPU_LAYERS: readInt('s-local-gpu-layers', -1),
            LOCAL_MODEL_CONTEXT_LENGTH: readInt('s-local-ctx', 16384),
            LOCAL_MODEL_CHAT_FORMAT: byId('s-local-chat-format').value,
            USE_LOCAL_MAIN: byId('s-local-main').checked,
            USE_LOCAL_CODE: byId('s-local-code').checked,
            USE_LOCAL_LIGHT: byId('s-local-light').checked,
            USE_LOCAL_FALLBACK: byId('s-local-fallback').checked,
            OPENAI_BASE_URL: byId('s-openai-base-url').value.trim(),
            OPENAI_COMPATIBLE_BASE_URL: byId('s-openai-compatible-base-url').value.trim(),
            CLOUDRU_FOUNDATION_MODELS_BASE_URL: byId('s-cloudru-base-url').value.trim(),
            ...collectMcpSettings(),
        };

        page.querySelectorAll('[data-secret-setting]').forEach((input) => {
            collectSecretValue(input.id, body);
        });
        page.querySelectorAll('[data-custom-secret-row]').forEach((row) => {
            const keyInput = row.querySelector('[data-custom-secret-key]');
            const valueInput = row.querySelector('[data-custom-secret-value]');
            const key = (keyInput?.value || '').trim().toUpperCase();
            const error = row.querySelector('[data-custom-secret-error]');
            if (!key) return;
            if (!/^[A-Z][A-Z0-9_]{2,}$/.test(key)) { if (error) { error.hidden = false; error.textContent = 'Use uppercase letters, numbers, and underscores.'; } return; }
            if (row.dataset.removeCustomSecret === '1' || valueInput?.dataset.forceClear === '1') { body[key] = ''; return; }
            const value = valueInput?.value || '';
            if (value && !value.includes('...')) body[key] = value;
        });

        return body;
    }

    async function saveRuntimeModeViaNativeBridgeIfNeeded() {
        const nextMode = byId('s-runtime-mode').value || 'advanced';
        const currentMode = currentSettings?.OUROBOROS_RUNTIME_MODE || 'advanced';
        if (nextMode === currentMode) return null;
        const bridge = window.pywebview?.api?.request_runtime_mode_change;
        if (!bridge) {
            throw new Error(
                'Runtime mode changes require the desktop launcher confirmation bridge. '
                + 'Use the desktop app, or stop Ouroboros and edit settings.json manually.'
            );
        }
        const result = await bridge(nextMode);
        if (!result || result.ok !== true) {
            throw new Error(result?.error || 'Runtime mode change was cancelled.');
        }
        return result;
    }

    async function saveAutoGrantViaNativeBridgeIfNeeded() {
        const checkbox = byId('s-auto-grant-reviewed-skills');
        if (!checkbox) return null;
        const nextEnabled = Boolean(checkbox.checked);
        const currentEnabled = isTruthySetting(currentSettings?.OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS);
        if (nextEnabled === currentEnabled) return null;
        const bridge = window.pywebview?.api?.request_auto_grant_reviewed_skills_change;
        if (!bridge) {
            throw new Error(
                'Reviewed-skill auto-grant changes require the desktop launcher confirmation bridge. '
                + 'Use the desktop app, or stop Ouroboros and edit settings.json manually.'
            );
        }
        const result = await bridge(nextEnabled);
        if (!result || result.ok !== true) {
            throw new Error(result?.error || 'Reviewed-skill auto-grant change was cancelled.');
        }
        return result;
    }

    syncSettingsLoadState();
    syncRuntimeModeBridgeState();
    syncAutoGrantBridgeState();
    window.addEventListener('pywebviewready', () => {
        syncRuntimeModeBridgeState();
        syncAutoGrantBridgeState();
    });
    [250, 1000, 2500].forEach((delay) => {
        setTimeout(() => {
            syncRuntimeModeBridgeState();
            syncAutoGrantBridgeState();
        }, delay);
    });
    reloadSettingsWithFeedback();

    if (typeof setBeforePageLeave === 'function') {
        setBeforePageLeave(({ from }) => {
            if (from !== 'settings' || !settingsDirty) return true;
            const leave = confirm('You have unsaved settings changes. Discard them and leave Settings?');
            if (leave) discardUnsavedSettingsDraft();
            return leave;
        });
    }

    byId('s-anthropic')?.addEventListener('input', () => {
        renderClaudeCodeUi();
        if (anthropicKeyConfigured()) {
            startClaudeCodePolling();
            refreshClaudeCodeStatus();
        }
    });

    page.addEventListener('input', updateSettingsDirtyState);
    page.addEventListener('change', updateSettingsDirtyState);
    page.addEventListener('click', (event) => {
        if (event.target.closest('[data-effort-value], .secret-clear, [data-row-secret-clear], [data-custom-secret-remove]')) {
            queueMicrotask(updateSettingsDirtyState);
        }
    });
    byId('btn-add-custom-secret')?.addEventListener('click', () => {
        const host = byId('custom-secrets-list');
        if (!host) return;
        if (host.querySelector('.muted')) host.innerHTML = '';
        const row = customSecretRow();
        host.appendChild(row);
        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
        row.querySelector('[data-custom-secret-key]')?.focus();
        markSettingsDirty();
    });

    window.addEventListener('ouro:skill-lifecycle', (event) => {
        const action = String(event.detail?.action || 'skills changed');
        refreshSettingsAfterExtensionChange(action);
    });
    if (ws && typeof ws.on === 'function') {
        ws.on('extension_lifecycle', (event) => {
            const action = String(event?.action || 'extension lifecycle');
            refreshSettingsAfterExtensionChange(action);
        });
    }

    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'settings') refreshSettingsAfterExtensionChange('settings page shown');
    });

    function closeSettingsModelPickers(exceptPicker = null) {
        page.querySelectorAll('[data-model-picker]').forEach((picker) => {
            if (picker === exceptPicker) return;
            const panel = picker.querySelector('.model-picker-results');
            if (!panel) return;
            panel.hidden = true;
            panel.innerHTML = '';
        });
    }

    function renderSettingsModelPicker(input) {
        const picker = input.closest('[data-model-picker]');
        const panel = picker?.querySelector('.model-picker-results');
        if (!picker || !panel) return;
        const needle = String(input.value || '').trim().toLowerCase();
        let items = settingsModelCatalogItems
            .filter((item) => {
                const haystack = `${item.value} ${item.label || ''} ${item.provider || ''}`.toLowerCase();
                return !needle || haystack.includes(needle);
            })
            .slice(0, 8);
        if (!items.length && needle) {
            items = settingsModelCatalogItems.slice(0, 8);
        }
        if (!items.length) {
            panel.hidden = true;
            panel.innerHTML = '';
            return;
        }
        panel.innerHTML = items.map((item) => `
            <button type="button" class="model-picker-item" data-value="${escapeHtml(item.value)}">
                <span class="model-picker-item-value">${escapeHtml(item.value)}</span>
                <span class="model-picker-item-label">${escapeHtml(item.label || item.provider || 'Catalog model')}</span>
            </button>
        `).join('');
        panel.hidden = false;
    }

    page.addEventListener('focusin', (event) => {
        const input = event.target instanceof Element
            ? event.target.closest('[data-model-picker] input')
            : null;
        if (!input) return;
        const picker = input.closest('[data-model-picker]');
        closeSettingsModelPickers(picker);
        renderSettingsModelPicker(input);
    });
    page.dataset.modelPickerBound = '1';

    page.addEventListener('input', (event) => {
        const input = event.target instanceof Element
            ? event.target.closest('[data-model-picker] input')
            : null;
        if (!input) return;
        const picker = input.closest('[data-model-picker]');
        closeSettingsModelPickers(picker);
        renderSettingsModelPicker(input);
    });

    page.addEventListener('mousedown', (event) => {
        const item = event.target instanceof Element
            ? event.target.closest('.model-picker-item')
            : null;
        if (item) {
            const picker = item.closest('[data-model-picker]');
            const input = picker?.querySelector('input');
            if (input) {
                event.preventDefault();
                input.value = item.dataset.value || '';
                closeSettingsModelPickers();
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }
            return;
        }
        if (!(event.target instanceof Element) || !event.target.closest('[data-model-picker]')) {
            closeSettingsModelPickers();
        }
    });

    document.addEventListener('settings-model-catalog:updated', (event) => {
        const items = Array.isArray(event.detail?.items) ? event.detail.items : [];
        settingsModelCatalogItems = items.length
            ? items.map((item) => ({
                value: item.value || item.id || '',
                label: item.label || item.provider || 'Catalog model',
                provider: item.provider || '',
            })).filter((item) => item.value)
            : SETTINGS_FALLBACK_MODELS.map((value) => ({ value, label: 'Suggested model' }));
        page.querySelectorAll('[data-model-picker]').forEach((picker) => {
            const panel = picker.querySelector('.model-picker-results');
            if (panel && !panel.hidden) {
                const input = picker.querySelector('input');
                renderSettingsModelPicker(input);
            }
        });
    });

    page.addEventListener('click', (event) => {
        if (event.target.closest('.secret-clear[data-target="s-anthropic"]')) {
            queueMicrotask(() => {
                renderClaudeCodeUi();
                refreshClaudeCodeStatus();
            });
        }
    });

    byId('btn-claude-code-install')?.addEventListener('click', async () => {
        applyClaudeCodeStatus({
            installed: false,
            ready: false,
            busy: true,
            message: 'Repairing Claude runtime...',
            error: '',
        });
        try {
            const resp = await apiFetch('/api/claude-code/install', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            applyClaudeCodeStatus(data);
            setStatus(data.repaired ? 'Claude runtime repaired' : 'Claude runtime up to date', 'ok');
        } catch (error) {
            const message = String(error?.message || error || '');
            applyClaudeCodeStatus({
                installed: false,
                ready: false,
                busy: false,
                error: message,
                message: `Claude runtime repair failed: ${message}`,
            });
            setStatus('Claude runtime repair failed', 'warn');
        }
    });

    byId('btn-refresh-model-catalog').addEventListener('click', async () => {
        await refreshModelCatalog();
    });

    byId('btn-reload-settings')?.addEventListener('click', async () => {
        await reloadSettingsWithFeedback();
    });

    byId('btn-save-settings').addEventListener('click', async () => {
        if (!settingsLoaded) {
            setStatus('Reload current settings successfully before saving.', 'warn');
            return;
        }
        const body = collectBody();

        try {
            const resp = await apiFetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            let runtimeModeResult = null;
            let runtimeModeError = '';
            let autoGrantResult = null;
            let autoGrantError = '';
            try {
                runtimeModeResult = await saveRuntimeModeViaNativeBridgeIfNeeded();
            } catch (error) {
                runtimeModeError = error.message || String(error);
            }
            try {
                autoGrantResult = await saveAutoGrantViaNativeBridgeIfNeeded();
            } catch (error) {
                autoGrantError = error.message || String(error);
            }
            await loadSettings();
            syncAutoGrantBridgeState();
            let statusMsg;
            let statusType = 'ok';
            if (data.no_changes) {
                statusMsg = 'No changes detected';
            } else if (data.restart_required) {
                statusMsg = 'Settings saved. Some changes require a restart to take effect';
                statusType = 'warn';
            } else if (data.immediate_changed && data.next_task_changed) {
                statusMsg = 'Settings saved. Some changes took effect immediately; others apply on the next task';
            } else if (data.immediate_changed) {
                statusMsg = 'Settings saved. Changes took effect immediately';
            } else {
                statusMsg = 'Settings saved. Changes take effect on the next task';
            }
            if (data.warnings && data.warnings.length) {
                statusMsg += ' ⚠️ ' + data.warnings.join(' | ');
                statusType = 'warn';
            }
            if (runtimeModeResult?.restart_required) {
                statusMsg = `${statusMsg} Runtime mode saved as ${runtimeModeResult.runtime_mode}; restart required.`;
                statusType = 'warn';
            }
            if (runtimeModeError) {
                statusMsg = `${statusMsg} Runtime mode was not changed: ${runtimeModeError}`;
                statusType = 'warn';
            }
            if (autoGrantResult) {
                statusMsg = `${statusMsg} Reviewed-skill auto-grant ${autoGrantResult.enabled ? 'enabled' : 'disabled'}.`;
            }
            if (autoGrantError) {
                statusMsg = `${statusMsg} Reviewed-skill auto-grant was not changed: ${autoGrantError}`;
                statusType = 'warn';
            }
            setStatus(statusMsg, statusType);
        } catch (e) {
            setStatus('Failed to save: ' + e.message, 'warn');
        }
    });

    byId('btn-reset').addEventListener('click', async () => {
        if (!confirm('This will delete all runtime data (state, memory, logs, settings) and restart.\nThe repo (agent code) will be preserved.\nYou will need to re-enter your provider settings.\n\nContinue?')) return;
        try {
            const res = await apiFetch('/api/reset', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'ok') alert('Deleted: ' + (data.deleted.join(', ') || 'nothing') + '\nRestarting...');
            else alert('Error: ' + (data.error || 'unknown'));
        } catch (e) {
            showToast('Reset failed: ' + e.message, 'error');
        }
    });

    return {
        activateTab: activateSettingsTab,
        page,
    };
}
