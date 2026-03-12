export function initSettings({ ws, state }) {
    const page = document.createElement('div');
    page.id = 'page-settings';
    page.className = 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg>
            <h2>Settings</h2>
        </div>
        <div class="settings-scroll">
            <div class="form-section">
                <h3>API Keys</h3>
                <div class="form-row"><div class="form-field"><label>OpenRouter API Key</label><input id="s-openrouter" type="password" placeholder="sk-or-..."></div></div>
                <div class="form-row"><div class="form-field"><label>OpenAI API Key (optional)</label><input id="s-openai" type="password"></div></div>
                <div class="form-row"><div class="form-field"><label>Anthropic API Key (optional)</label><input id="s-anthropic" type="password"></div></div>
            </div>
            <div class="divider"></div>
            <div class="form-section">
                <h3>Local Model</h3>
                <div class="form-row">
                    <div class="form-field"><label>Model Source</label><input id="s-local-source" placeholder="bartowski/Llama-3.3-70B-Instruct-GGUF or /path/to/model.gguf" style="width:400px"></div>
                </div>
                <div class="form-row">
                    <div class="form-field"><label>GGUF Filename (for HF repos)</label><input id="s-local-filename" placeholder="Llama-3.3-70B-Instruct-Q4_K_M.gguf" style="width:400px"></div>
                </div>
                <div class="form-row">
                    <div class="form-field"><label>Port</label><input id="s-local-port" type="number" value="8766" style="width:100px"></div>
                    <div class="form-field"><label>GPU Layers (-1 = all)</label><input id="s-local-gpu-layers" type="number" value="-1" style="width:100px"></div>
                    <div class="form-field"><label>Context Length</label><input id="s-local-ctx" type="number" value="16384" style="width:120px" placeholder="16384"></div>
                    <div class="form-field"><label>Chat Format</label><input id="s-local-chat-format" value="" placeholder="auto-detect" style="width:200px"></div>
                </div>
                <div class="form-row" style="align-items:center;gap:8px">
                    <button class="btn btn-primary" id="btn-local-start">Start</button>
                    <button class="btn btn-primary" id="btn-local-stop" style="opacity:0.5">Stop</button>
                    <button class="btn btn-primary" id="btn-local-test" style="opacity:0.5">Test Tool Calling</button>
                </div>
                <div id="local-model-status" style="margin-top:8px;font-size:13px;color:var(--text-secondary)">Status: Offline</div>
                <div id="local-model-test-result" style="margin-top:4px;font-size:12px;color:var(--text-muted);display:none"></div>
            </div>
            <div class="divider"></div>
            <div class="form-section">
                <h3>Models</h3>
                <div style="margin:0 0 8px 0;font-size:12px;color:var(--text-secondary)">
                    These fields are cloud model IDs. Enable <code>Local</code> to route that lane
                    through the GGUF server configured above.
                </div>
                <div class="form-row" style="align-items:flex-end">
                    <div class="form-field"><label>Main Model</label><input id="s-model" value="anthropic/claude-sonnet-4.6" style="width:250px"></div>
                    <label class="local-toggle"><input type="checkbox" id="s-local-main"> Local</label>
                </div>
                <div class="form-row" style="align-items:flex-end">
                    <div class="form-field"><label>Code Model</label><input id="s-model-code" value="anthropic/claude-sonnet-4.6" style="width:250px"></div>
                    <label class="local-toggle"><input type="checkbox" id="s-local-code"> Local</label>
                </div>
                <div class="form-row" style="align-items:flex-end">
                    <div class="form-field"><label>Light Model</label><input id="s-model-light" value="google/gemini-3-flash-preview" style="width:250px"></div>
                    <label class="local-toggle"><input type="checkbox" id="s-local-light"> Local</label>
                </div>
                <div class="form-row" style="align-items:flex-end">
                    <div class="form-field"><label>Fallback Model</label><input id="s-model-fallback" value="google/gemini-3-flash-preview" style="width:250px"></div>
                    <label class="local-toggle"><input type="checkbox" id="s-local-fallback"> Local</label>
                </div>
                <div class="form-row">
                    <div class="form-field"><label>Claude Code Model</label><input id="s-claude-code-model" value="sonnet" placeholder="sonnet, opus, or full name" style="width:250px"></div>
                </div>
            </div>
            <div class="divider"></div>
            <div class="form-section">
                <h3>Runtime</h3>
                <div class="form-row">
                    <div class="form-field"><label>Max Workers</label><input id="s-workers" type="number" min="1" max="10" value="5" style="width:100px"></div>
                    <div class="form-field"><label>Total Budget ($)</label><input id="s-budget" type="number" min="1" value="10" style="width:120px"></div>
                </div>
                <div class="form-row">
                    <div class="form-field"><label>Soft Timeout (s)</label><input id="s-soft-timeout" type="number" value="600" style="width:120px"></div>
                    <div class="form-field"><label>Hard Timeout (s)</label><input id="s-hard-timeout" type="number" value="1800" style="width:120px"></div>
                </div>
                <div class="form-row">
                    <div class="form-field"><label>Tool Timeout (s)</label><input id="s-tool-timeout" type="number" value="120" style="width:120px"></div>
                </div>
            </div>
            <div class="divider"></div>
            <div class="form-section">
                <h3>GitHub (optional)</h3>
                <div class="form-row"><div class="form-field"><label>GitHub Token</label><input id="s-gh-token" type="password" placeholder="ghp_..."></div></div>
                <div class="form-row"><div class="form-field"><label>GitHub Repo</label><input id="s-gh-repo" placeholder="owner/repo-name"></div></div>
            </div>
            <div class="divider"></div>
            <div class="form-row">
                <button class="btn btn-save" id="btn-save-settings">Save Settings</button>
            </div>
            <div id="settings-status" style="margin-top:8px;font-size:13px;color:var(--green);display:none"></div>
            <div class="divider"></div>
            <div class="form-section">
                <h3 style="color:var(--red)">Danger Zone</h3>
                <button class="btn btn-danger" id="btn-reset">Reset All Data</button>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    const secretInputIds = ['s-openrouter', 's-openai', 's-anthropic', 's-gh-token'];
    secretInputIds.forEach((id) => {
        const input = document.getElementById(id);
        input.addEventListener('focus', () => {
            if (input.value.includes('...')) input.value = '';
        });
    });

    function applySettings(s) {
        if (s.OPENROUTER_API_KEY) document.getElementById('s-openrouter').value = s.OPENROUTER_API_KEY;
        if (s.OPENAI_API_KEY) document.getElementById('s-openai').value = s.OPENAI_API_KEY;
        if (s.ANTHROPIC_API_KEY) document.getElementById('s-anthropic').value = s.ANTHROPIC_API_KEY;
        if (s.OUROBOROS_MODEL) document.getElementById('s-model').value = s.OUROBOROS_MODEL;
        if (s.OUROBOROS_MODEL_CODE) document.getElementById('s-model-code').value = s.OUROBOROS_MODEL_CODE;
        if (s.OUROBOROS_MODEL_LIGHT) document.getElementById('s-model-light').value = s.OUROBOROS_MODEL_LIGHT;
        if (s.OUROBOROS_MODEL_FALLBACK) document.getElementById('s-model-fallback').value = s.OUROBOROS_MODEL_FALLBACK;
        if (s.CLAUDE_CODE_MODEL) document.getElementById('s-claude-code-model').value = s.CLAUDE_CODE_MODEL;
        if (s.OUROBOROS_MAX_WORKERS) document.getElementById('s-workers').value = s.OUROBOROS_MAX_WORKERS;
        if (s.TOTAL_BUDGET) document.getElementById('s-budget').value = s.TOTAL_BUDGET;
        if (s.OUROBOROS_SOFT_TIMEOUT_SEC) document.getElementById('s-soft-timeout').value = s.OUROBOROS_SOFT_TIMEOUT_SEC;
        if (s.OUROBOROS_HARD_TIMEOUT_SEC) document.getElementById('s-hard-timeout').value = s.OUROBOROS_HARD_TIMEOUT_SEC;
        if (s.OUROBOROS_TOOL_TIMEOUT_SEC) document.getElementById('s-tool-timeout').value = s.OUROBOROS_TOOL_TIMEOUT_SEC;
        if (s.GITHUB_TOKEN) document.getElementById('s-gh-token').value = s.GITHUB_TOKEN;
        if (s.GITHUB_REPO) document.getElementById('s-gh-repo').value = s.GITHUB_REPO;
        if (s.LOCAL_MODEL_SOURCE) document.getElementById('s-local-source').value = s.LOCAL_MODEL_SOURCE;
        if (s.LOCAL_MODEL_FILENAME) document.getElementById('s-local-filename').value = s.LOCAL_MODEL_FILENAME;
        if (s.LOCAL_MODEL_PORT) document.getElementById('s-local-port').value = s.LOCAL_MODEL_PORT;
        if (s.LOCAL_MODEL_N_GPU_LAYERS != null) document.getElementById('s-local-gpu-layers').value = s.LOCAL_MODEL_N_GPU_LAYERS;
        if (s.LOCAL_MODEL_CONTEXT_LENGTH) document.getElementById('s-local-ctx').value = s.LOCAL_MODEL_CONTEXT_LENGTH;
        if (s.LOCAL_MODEL_CHAT_FORMAT) document.getElementById('s-local-chat-format').value = s.LOCAL_MODEL_CHAT_FORMAT;
        document.getElementById('s-local-main').checked = s.USE_LOCAL_MAIN === true || s.USE_LOCAL_MAIN === 'True';
        document.getElementById('s-local-code').checked = s.USE_LOCAL_CODE === true || s.USE_LOCAL_CODE === 'True';
        document.getElementById('s-local-light').checked = s.USE_LOCAL_LIGHT === true || s.USE_LOCAL_LIGHT === 'True';
        document.getElementById('s-local-fallback').checked = s.USE_LOCAL_FALLBACK === true || s.USE_LOCAL_FALLBACK === 'True';
    }

    async function loadSettings() {
        const resp = await fetch('/api/settings');
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        applySettings(data);
    }

    loadSettings().catch(() => {});

    let localStatusInterval = null;
    function updateLocalStatus() {
        if (state.activePage !== 'settings') return; // Don't poll if page is hidden
        fetch('/api/local-model/status').then(r => r.json()).then(d => {
            const el = document.getElementById('local-model-status');
            const isReady = d.status === 'ready';
            let text = 'Status: ' + (d.status || 'offline').charAt(0).toUpperCase() + (d.status || 'offline').slice(1);
            if (d.status === 'ready' && d.context_length) text += ` (ctx: ${d.context_length})`;
            if (d.status === 'downloading' && d.download_progress) text += ` ${Math.round(d.download_progress * 100)}%`;
            if (d.error) text += ' \u2014 ' + d.error;
            el.textContent = text;
            el.style.color = isReady ? 'var(--green)' : d.status === 'error' ? 'var(--red)' : 'var(--text-secondary)';
            document.getElementById('btn-local-stop').style.opacity = isReady ? '1' : '0.5';
            document.getElementById('btn-local-test').style.opacity = isReady ? '1' : '0.5';
        }).catch(() => {});
    }
    updateLocalStatus();
    localStatusInterval = setInterval(updateLocalStatus, 3000);

    document.getElementById('btn-local-start').addEventListener('click', async () => {
        const source = document.getElementById('s-local-source').value.trim();
        if (!source) { alert('Enter a model source (HuggingFace repo ID or local path)'); return; }
        const body = {
            source,
            filename: document.getElementById('s-local-filename').value.trim(),
            port: parseInt(document.getElementById('s-local-port').value) || 8766,
            n_gpu_layers: parseInt(document.getElementById('s-local-gpu-layers').value),
            n_ctx: parseInt(document.getElementById('s-local-ctx').value) || 16384,
            chat_format: document.getElementById('s-local-chat-format').value.trim(),
        };
        try {
            const resp = await fetch('/api/local-model/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const data = await resp.json();
            if (data.error) alert('Error: ' + data.error);
            else updateLocalStatus();
        } catch (e) { alert('Failed: ' + e.message); }
    });

    document.getElementById('btn-local-stop').addEventListener('click', async () => {
        try {
            await fetch('/api/local-model/stop', { method: 'POST' });
            updateLocalStatus();
        } catch (e) { alert('Failed: ' + e.message); }
    });

    document.getElementById('btn-local-test').addEventListener('click', async () => {
        const el = document.getElementById('local-model-test-result');
        el.style.display = 'block';
        el.textContent = 'Running tests...';
        el.style.color = 'var(--text-muted)';
        try {
            const resp = await fetch('/api/local-model/test', { method: 'POST' });
            const r = await resp.json();
            if (r.error) { el.textContent = 'Error: ' + r.error; el.style.color = 'var(--red)'; return; }
            let lines = [];
            lines.push((r.chat_ok ? '\u2713' : '\u2717') + ' Basic chat' + (r.tokens_per_sec ? ` (${r.tokens_per_sec} tok/s)` : ''));
            lines.push((r.tool_call_ok ? '\u2713' : '\u2717') + ' Tool calling');
            if (r.details && !r.success) lines.push(r.details);
            el.textContent = lines.join('\n');
            el.style.whiteSpace = 'pre-wrap';
            el.style.color = r.success ? 'var(--green)' : 'var(--amber)';
        } catch (e) { el.textContent = 'Test failed: ' + e.message; el.style.color = 'var(--red)'; }
    });

    document.getElementById('btn-save-settings').addEventListener('click', async () => {
        const body = {
            OUROBOROS_MODEL: document.getElementById('s-model').value,
            OUROBOROS_MODEL_CODE: document.getElementById('s-model-code').value,
            OUROBOROS_MODEL_LIGHT: document.getElementById('s-model-light').value,
            OUROBOROS_MODEL_FALLBACK: document.getElementById('s-model-fallback').value,
            CLAUDE_CODE_MODEL: document.getElementById('s-claude-code-model').value || 'sonnet',
            OUROBOROS_MAX_WORKERS: parseInt(document.getElementById('s-workers').value) || 5,
            TOTAL_BUDGET: parseFloat(document.getElementById('s-budget').value) || 10,
            OUROBOROS_SOFT_TIMEOUT_SEC: parseInt(document.getElementById('s-soft-timeout').value) || 600,
            OUROBOROS_HARD_TIMEOUT_SEC: parseInt(document.getElementById('s-hard-timeout').value) || 1800,
            OUROBOROS_TOOL_TIMEOUT_SEC: parseInt(document.getElementById('s-tool-timeout').value) || 120,
            GITHUB_REPO: document.getElementById('s-gh-repo').value,
            LOCAL_MODEL_SOURCE: document.getElementById('s-local-source').value,
            LOCAL_MODEL_FILENAME: document.getElementById('s-local-filename').value,
            LOCAL_MODEL_PORT: parseInt(document.getElementById('s-local-port').value) || 8766,
            LOCAL_MODEL_N_GPU_LAYERS: parseInt(document.getElementById('s-local-gpu-layers').value),
            LOCAL_MODEL_CONTEXT_LENGTH: parseInt(document.getElementById('s-local-ctx').value) || 16384,
            LOCAL_MODEL_CHAT_FORMAT: document.getElementById('s-local-chat-format').value,
            USE_LOCAL_MAIN: document.getElementById('s-local-main').checked,
            USE_LOCAL_CODE: document.getElementById('s-local-code').checked,
            USE_LOCAL_LIGHT: document.getElementById('s-local-light').checked,
            USE_LOCAL_FALLBACK: document.getElementById('s-local-fallback').checked,
        };
        const orKey = document.getElementById('s-openrouter').value;
        if (orKey && !orKey.includes('...')) body.OPENROUTER_API_KEY = orKey;
        const oaiKey = document.getElementById('s-openai').value;
        if (oaiKey && !oaiKey.includes('...')) body.OPENAI_API_KEY = oaiKey;
        const antKey = document.getElementById('s-anthropic').value;
        if (antKey && !antKey.includes('...')) body.ANTHROPIC_API_KEY = antKey;
        const ghToken = document.getElementById('s-gh-token').value;
        if (ghToken && !ghToken.includes('...')) body.GITHUB_TOKEN = ghToken;

        try {
            const resp = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            await loadSettings();
            const status = document.getElementById('settings-status');
            status.textContent = data.warnings && data.warnings.length
                ? ('Settings saved with warnings: ' + data.warnings.join(' | '))
                : 'Settings saved. Budget changes take effect immediately.';
            status.style.color = data.warnings && data.warnings.length ? 'var(--amber)' : 'var(--green)';
            status.style.display = 'block';
            setTimeout(() => status.style.display = 'none', 4000);
        } catch (e) {
            alert('Failed to save: ' + e.message);
        }
    });

    document.getElementById('btn-reset').addEventListener('click', async () => {
        if (!confirm('This will delete all runtime data (state, memory, logs, settings) and restart.\nThe repo (agent code) will be preserved.\nYou will need to re-enter your API key.\n\nContinue?')) return;
        try {
            const res = await fetch('/api/reset', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'ok') {
                alert('Deleted: ' + (data.deleted.join(', ') || 'nothing') + '\nRestarting...');
            } else {
                alert('Error: ' + (data.error || 'unknown'));
            }
        } catch (e) {
            alert('Reset failed: ' + e.message);
        }
    });

}
