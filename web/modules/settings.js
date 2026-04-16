import { refreshModelCatalog } from './settings_catalog.js';
import { bindEffortSegments, bindModelPickers, syncEffortSegments } from './settings_controls.js';
import { bindLocalModelControls } from './settings_local_model.js';
import { bindSecretInputs, bindSettingsTabs, renderSettingsPage } from './settings_ui.js';

function byId(id) {
    return document.getElementById(id);
}

function applyInputValue(id, value) {
    byId(id).value = value === undefined || value === null ? '' : value;
}

function applyCheckboxValue(id, value) {
    byId(id).checked = value === true || value === 'True';
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

export function initSettings({ state }) {
    const page = document.createElement('div');
    page.id = 'page-settings';
    page.className = 'page';
    page.innerHTML = renderSettingsPage();
    document.getElementById('content').appendChild(page);

    bindSettingsTabs(page);
    bindSecretInputs(page);
    bindEffortSegments(page);
    bindModelPickers(page);
    bindLocalModelControls({ state });
    let currentSettings = {};
    let claudeCodePollStarted = false;
    // v4.33.1 status_label priority fix: even when the user has not configured
    // ANTHROPIC_API_KEY, we still surface the runtime card when the backend
    // reports status="error" (e.g. SDK below baseline). Otherwise a version-gate
    // failure is silently hidden until the user adds a key, which defeats the
    // whole point of prioritizing error over no_api_key in `status_label`.
    let claudeRuntimeHasError = false;

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
            const resp = await fetch('/api/claude-code/status', { cache: 'no-store' });
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
        // LAN Access checkbox: checked when host is 0.0.0.0
        const lanCheckbox = byId('s-lan-access');
        if (lanCheckbox) {
            lanCheckbox.checked = (s.OUROBOROS_SERVER_HOST === '0.0.0.0');
        }
        applyInputValue('s-telegram-token', s.TELEGRAM_BOT_TOKEN);
        applyInputValue('s-telegram-chat-id', s.TELEGRAM_CHAT_ID);

        applyInputValue('s-model', s.OUROBOROS_MODEL);
        applyInputValue('s-model-code', s.OUROBOROS_MODEL_CODE);
        applyInputValue('s-model-light', s.OUROBOROS_MODEL_LIGHT);
        applyInputValue('s-model-fallback', s.OUROBOROS_MODEL_FALLBACK);
        applyInputValue('s-claude-code-model', s.CLAUDE_CODE_MODEL);
        byId('s-effort-task').value = s.OUROBOROS_EFFORT_TASK || s.OUROBOROS_INITIAL_REASONING_EFFORT || 'medium';
        byId('s-effort-evolution').value = s.OUROBOROS_EFFORT_EVOLUTION || 'high';
        byId('s-effort-review').value = s.OUROBOROS_EFFORT_REVIEW || 'medium';
        byId('s-effort-consciousness').value = s.OUROBOROS_EFFORT_CONSCIOUSNESS || 'low';
        applyInputValue('s-review-models', s.OUROBOROS_REVIEW_MODELS);
        applyInputValue('s-scope-review-model', s.OUROBOROS_SCOPE_REVIEW_MODEL);
        byId('s-effort-scope-review').value = s.OUROBOROS_EFFORT_SCOPE_REVIEW || 'high';
        byId('s-review-enforcement').value = s.OUROBOROS_REVIEW_ENFORCEMENT || 'advisory';
        if (s.OUROBOROS_MAX_WORKERS) byId('s-workers').value = s.OUROBOROS_MAX_WORKERS;
        if (s.OUROBOROS_SOFT_TIMEOUT_SEC) byId('s-soft-timeout').value = s.OUROBOROS_SOFT_TIMEOUT_SEC;
        if (s.OUROBOROS_HARD_TIMEOUT_SEC) byId('s-hard-timeout').value = s.OUROBOROS_HARD_TIMEOUT_SEC;
        if (s.OUROBOROS_TOOL_TIMEOUT_SEC) byId('s-tool-timeout').value = s.OUROBOROS_TOOL_TIMEOUT_SEC;
        applyInputValue('s-websearch-model', s.OUROBOROS_WEBSEARCH_MODEL);
        applyInputValue('s-gh-token', s.GITHUB_TOKEN);
        applyInputValue('s-gh-repo', s.GITHUB_REPO);
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
        // A2A settings
        applyCheckboxValue('s-a2a-enabled', s.A2A_ENABLED);
        if (s.A2A_PORT) applyInputValue('s-a2a-port', s.A2A_PORT);
        applyInputValue('s-a2a-host', s.A2A_HOST);
        applyInputValue('s-a2a-agent-name', s.A2A_AGENT_NAME);
        applyInputValue('s-a2a-agent-description', s.A2A_AGENT_DESCRIPTION);
        if (s.A2A_MAX_CONCURRENT) applyInputValue('s-a2a-max-concurrent', s.A2A_MAX_CONCURRENT);
        if (s.A2A_TASK_TTL_HOURS) applyInputValue('s-a2a-ttl-hours', s.A2A_TASK_TTL_HOURS);
        resetSecretClearFlags(page);
        syncEffortSegments(page);
    }

    async function loadSettings() {
        const resp = await fetch('/api/settings', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        currentSettings = data;
        applySettings(data);
        renderClaudeCodeUi();
        // Always start polling so a below-baseline SDK surfaces even before
        // the user sets ANTHROPIC_API_KEY. `refreshClaudeCodeStatus` is now
        // unconditional, and `shouldShowClaudeRuntimeCard` uses the runtime
        // error signal to decide visibility.
        startClaudeCodePolling();
    }

    function collectBody() {
        const body = {
            OUROBOROS_MODEL: byId('s-model').value,
            OUROBOROS_MODEL_CODE: byId('s-model-code').value,
            OUROBOROS_MODEL_LIGHT: byId('s-model-light').value,
            OUROBOROS_MODEL_FALLBACK: byId('s-model-fallback').value,
            CLAUDE_CODE_MODEL: byId('s-claude-code-model').value || 'claude-opus-4-7[1m]',
            OUROBOROS_EFFORT_TASK: byId('s-effort-task').value,
            OUROBOROS_EFFORT_EVOLUTION: byId('s-effort-evolution').value,
            OUROBOROS_EFFORT_REVIEW: byId('s-effort-review').value,
            OUROBOROS_EFFORT_CONSCIOUSNESS: byId('s-effort-consciousness').value,
            OUROBOROS_REVIEW_MODELS: byId('s-review-models').value.trim(),
            OUROBOROS_SCOPE_REVIEW_MODEL: byId('s-scope-review-model').value.trim(),
            OUROBOROS_EFFORT_SCOPE_REVIEW: byId('s-effort-scope-review').value,
            OUROBOROS_REVIEW_ENFORCEMENT: byId('s-review-enforcement').value,
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
            // A2A settings
            A2A_ENABLED: byId('s-a2a-enabled')?.checked ?? false,
            A2A_PORT: readInt('s-a2a-port', 18800),
            A2A_HOST: (byId('s-a2a-host')?.value || '127.0.0.1').trim(),
            A2A_AGENT_NAME: (byId('s-a2a-agent-name')?.value || '').trim(),
            A2A_AGENT_DESCRIPTION: (byId('s-a2a-agent-description')?.value || '').trim(),
            A2A_MAX_CONCURRENT: readInt('s-a2a-max-concurrent', 3),
            A2A_TASK_TTL_HOURS: readInt('s-a2a-ttl-hours', 24),
            OPENAI_BASE_URL: byId('s-openai-base-url').value.trim(),
            OPENAI_COMPATIBLE_BASE_URL: byId('s-openai-compatible-base-url').value.trim(),
            CLOUDRU_FOUNDATION_MODELS_BASE_URL: byId('s-cloudru-base-url').value.trim(),
            TELEGRAM_CHAT_ID: byId('s-telegram-chat-id').value.trim(),
            OUROBOROS_SERVER_HOST: byId('s-lan-access')?.checked ? '0.0.0.0' : '127.0.0.1',
        };

        collectSecretValue('s-openrouter', body);
        collectSecretValue('s-openai', body);
        collectSecretValue('s-openai-compatible-key', body);
        collectSecretValue('s-cloudru-key', body);
        collectSecretValue('s-anthropic', body);
        collectSecretValue('s-network-password', body);
        collectSecretValue('s-telegram-token', body);
        collectSecretValue('s-gh-token', body);

        return body;
    }

    async function updateLanHint() {
        const hint = byId('settings-lan-hint');
        if (!hint) return;
        const checked = byId('s-lan-access')?.checked;
        if (!checked) { hint.style.display = 'none'; return; }
        hint.style.display = 'block';
        hint.textContent = 'Detecting LAN address…';
        try {
            const resp = await fetch('/api/network-info', { cache: 'no-store' });
            const data = await resp.json();
            const port = data.port || window.location.port || '8765';
            if (data.ips && data.ips.length) {
                hint.innerHTML = '📡 Access from other devices: ' +
                    data.ips.map(ip => `<a href="http://${ip}:${port}" target="_blank" class="settings-lan-link">http://${ip}:${port}</a>`).join(', ');
            } else {
                hint.textContent = '⚠️ Could not detect LAN address.';
            }
        } catch {
            hint.textContent = '⚠️ Could not detect LAN address.';
        }
    }

    byId('s-lan-access')?.addEventListener('change', updateLanHint);

    loadSettings()
        .then(() => { refreshModelCatalog(); updateLanHint(); })
        .catch(() => {});

    byId('s-anthropic')?.addEventListener('input', () => {
        renderClaudeCodeUi();
        if (anthropicKeyConfigured()) {
            startClaudeCodePolling();
            refreshClaudeCodeStatus();
        }
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
            const resp = await fetch('/api/claude-code/install', { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            applyClaudeCodeStatus(data);
            setStatus(data.repaired ? 'Claude runtime repaired.' : 'Claude runtime up to date.', 'ok');
        } catch (error) {
            const message = String(error?.message || error || '');
            applyClaudeCodeStatus({
                installed: false,
                ready: false,
                busy: false,
                error: message,
                message: `Claude runtime repair failed: ${message}`,
            });
            setStatus('Claude runtime repair failed.', 'warn');
        }
    });

    byId('btn-refresh-model-catalog').addEventListener('click', async () => {
        await refreshModelCatalog();
    });

    byId('btn-save-settings').addEventListener('click', async () => {
        const body = collectBody();

        try {
            const resp = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            await loadSettings();
            let statusMsg;
            let statusType = 'ok';
            if (data.no_changes) {
                statusMsg = 'No changes detected.';
            } else if (data.restart_required) {
                statusMsg = 'Settings saved. Some changes require a restart to take effect.';
                statusType = 'warn';
            } else if (data.immediate_changed && data.next_task_changed) {
                statusMsg = 'Settings saved. Some changes took effect immediately; others apply on the next task.';
            } else if (data.immediate_changed) {
                statusMsg = 'Settings saved. Changes took effect immediately.';
            } else {
                statusMsg = 'Settings saved. Changes take effect on the next task.';
            }
            if (data.warnings && data.warnings.length) {
                statusMsg += ' ⚠️ ' + data.warnings.join(' | ');
                statusType = 'warn';
            }
            setStatus(statusMsg, statusType);
        } catch (e) {
            alert('Failed to save: ' + e.message);
        }
    });

    byId('btn-reset').addEventListener('click', async () => {
        if (!confirm('This will delete all runtime data (state, memory, logs, settings) and restart.\nThe repo (agent code) will be preserved.\nYou will need to re-enter your provider settings.\n\nContinue?')) return;
        try {
            const res = await fetch('/api/reset', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'ok') alert('Deleted: ' + (data.deleted.join(', ') || 'nothing') + '\nRestarting...');
            else alert('Error: ' + (data.error || 'unknown'));
        } catch (e) {
            alert('Reset failed: ' + e.message);
        }
    });
}
