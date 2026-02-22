/**
 * Ouroboros Web UI — Main application.
 *
 * Self-editable: this file lives in REPO_DIR and can be modified by the agent.
 * Vanilla JS, no build step. Uses WebSocket for real-time communication.
 */

// ---------------------------------------------------------------------------
// WebSocket Manager
// ---------------------------------------------------------------------------
class WS {
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.listeners = {};
        this.reconnectDelay = 1000;
        this.maxDelay = 10000;
        this.connect();
    }

    connect() {
        this.ws = new WebSocket(this.url);
        this.ws.onopen = () => {
            this.reconnectDelay = 1000;
            this.emit('open');
            document.getElementById('reconnect-overlay')?.classList.remove('visible');
        };
        this.ws.onclose = () => {
            this.emit('close');
            document.getElementById('reconnect-overlay')?.classList.add('visible');
            setTimeout(() => this.connect(), this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxDelay);
        };
        this.ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                this.emit('message', msg);
                if (msg.type) this.emit(msg.type, msg);
            } catch {}
        };
    }

    send(msg) {
        if (this.ws?.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify(msg));
        }
    }

    on(event, fn) {
        (this.listeners[event] ||= []).push(fn);
    }

    emit(event, data) {
        (this.listeners[event] || []).forEach(fn => fn(data));
    }
}

const wsUrl = `ws://${location.host}/ws`;
const ws = new WS(wsUrl);

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
    messages: [],
    logs: [],
    dashboard: {},
    activeFilters: { tools: true, llm: true, errors: true, tasks: true, system: false, consciousness: false },
};

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------
function showPage(name) {
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(`page-${name}`)?.classList.add('active');
    document.querySelector(`.nav-btn[data-page="${name}"]`)?.classList.add('active');
}

document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => showPage(btn.dataset.page));
});

// ---------------------------------------------------------------------------
// Chat Page
// ---------------------------------------------------------------------------
function initChat() {
    const container = document.getElementById('content');

    const page = document.createElement('div');
    page.id = 'page-chat';
    page.className = 'page active';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            <h2>Chat</h2>
            <div class="spacer"></div>
            <span id="chat-status" class="status-badge offline">Connecting...</span>
        </div>
        <div id="chat-messages"></div>
        <div id="chat-input-area">
            <textarea id="chat-input" placeholder="Message Ouroboros..." rows="1"></textarea>
            <button class="icon-btn" id="chat-send">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
            </button>
        </div>
    `;
    container.appendChild(page);

    const messagesDiv = document.getElementById('chat-messages');
    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('chat-send');

    function addMessage(text, role, markdown = false) {
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}`;
        const sender = role === 'user' ? 'You' : 'Ouroboros';
        bubble.innerHTML = `
            <div class="sender">${sender}</div>
            <div class="message">${escapeHtml(text)}</div>
        `;
        messagesDiv.appendChild(bubble);
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function sendMessage() {
        const text = input.value.trim();
        if (!text) return;
        input.value = '';
        input.style.height = 'auto';
        addMessage(text, 'user');
        ws.send({ type: 'chat', content: text });
    }

    sendBtn.addEventListener('click', sendMessage);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });

    ws.on('chat', (msg) => {
        if (msg.role === 'assistant') {
            addMessage(msg.content, 'assistant', msg.markdown);
        }
    });

    ws.on('open', () => {
        document.getElementById('chat-status').className = 'status-badge online';
        document.getElementById('chat-status').textContent = 'Online';
    });
    ws.on('close', () => {
        document.getElementById('chat-status').className = 'status-badge offline';
        document.getElementById('chat-status').textContent = 'Reconnecting...';
    });

    addMessage('Welcome! Type a message or use /commands (/status, /evolve, /review, /bg, /restart).', 'assistant');
}

// ---------------------------------------------------------------------------
// Dashboard Page
// ---------------------------------------------------------------------------
function initDashboard() {
    const page = document.createElement('div');
    page.id = 'page-dashboard';
    page.className = 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
            <h2>Dashboard</h2>
        </div>
        <div class="dashboard-scroll">
            <h2 id="dash-title" style="font-size:24px;font-weight:700;margin-bottom:16px">Ouroboros</h2>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="label">Uptime</div>
                    <div class="value" id="dash-uptime">0s</div>
                </div>
                <div class="stat-card">
                    <div class="label">Workers</div>
                    <div class="value" id="dash-workers">...</div>
                    <div class="progress-bar"><div class="fill" id="dash-workers-bar" style="width:0;background:var(--accent)"></div></div>
                </div>
                <div class="stat-card">
                    <div class="label">Budget</div>
                    <div class="value" id="dash-budget">...</div>
                    <div class="progress-bar"><div class="fill" id="dash-budget-bar" style="width:0;background:var(--amber)"></div></div>
                </div>
                <div class="stat-card">
                    <div class="label">Branch</div>
                    <div class="value" id="dash-branch" style="color:var(--green)">ouroboros</div>
                </div>
            </div>
            <div class="divider"></div>
            <div class="section-title">Controls</div>
            <div class="controls-row">
                <div class="toggle-wrapper">
                    <button class="toggle" id="toggle-evo"></button>
                    <span class="toggle-label">Evolution Mode</span>
                </div>
                <div class="toggle-wrapper">
                    <button class="toggle" id="toggle-bg"></button>
                    <span class="toggle-label">Background Consciousness</span>
                </div>
            </div>
            <div class="controls-row">
                <button class="btn btn-default" id="btn-review">Force Review</button>
                <button class="btn btn-primary" id="btn-restart">Restart Agent</button>
                <button class="btn btn-danger" id="btn-panic">Panic Stop</button>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    document.getElementById('toggle-evo').addEventListener('click', function() {
        this.classList.toggle('on');
        ws.send({ type: 'command', cmd: `/evolve ${this.classList.contains('on') ? 'start' : 'stop'}` });
    });
    document.getElementById('toggle-bg').addEventListener('click', function() {
        this.classList.toggle('on');
        ws.send({ type: 'command', cmd: `/bg ${this.classList.contains('on') ? 'start' : 'stop'}` });
    });
    document.getElementById('btn-review').addEventListener('click', () => ws.send({ type: 'command', cmd: '/review' }));
    document.getElementById('btn-restart').addEventListener('click', () => ws.send({ type: 'command', cmd: '/restart' }));
    document.getElementById('btn-panic').addEventListener('click', () => {
        if (confirm('Kill all workers immediately?')) {
            ws.send({ type: 'command', cmd: '/panic' });
        }
    });

    // Poll dashboard state
    async function updateDashboard() {
        try {
            const resp = await fetch('/api/state');
            const data = await resp.json();
            const uptime = data.uptime || 0;
            const h = Math.floor(uptime / 3600);
            const m = Math.floor((uptime % 3600) / 60);
            const s = uptime % 60;
            document.getElementById('dash-uptime').textContent =
                h ? `${h}h ${m}m ${s}s` : m ? `${m}m ${s}s` : `${s}s`;

            document.getElementById('dash-workers').textContent =
                `${data.workers_alive || 0} / ${data.workers_total || 0} active`;
            const wPct = data.workers_total > 0 ? (data.workers_alive / data.workers_total * 100) : 0;
            document.getElementById('dash-workers-bar').style.width = `${wPct}%`;

            const spent = data.spent_usd || 0;
            const limit = data.budget_limit || 10;
            document.getElementById('dash-budget').textContent = `$${spent.toFixed(2)} / $${limit.toFixed(2)}`;
            document.getElementById('dash-budget-bar').style.width = `${Math.min(100, data.budget_pct || 0)}%`;

            document.getElementById('dash-branch').textContent =
                `${data.branch || 'ouroboros'}${data.sha ? '@' + data.sha : ''}`;

            if (data.evolution_enabled) document.getElementById('toggle-evo').classList.add('on');
            else document.getElementById('toggle-evo').classList.remove('on');
        } catch {}
    }

    updateDashboard();
    setInterval(updateDashboard, 3000);
}

// ---------------------------------------------------------------------------
// Settings Page
// ---------------------------------------------------------------------------
function initSettings() {
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
                <h3>Models</h3>
                <div class="form-row">
                    <div class="form-field"><label>Main Model</label><input id="s-model" value="anthropic/claude-sonnet-4.6" style="width:250px"></div>
                    <div class="form-field"><label>Code Model</label><input id="s-model-code" value="anthropic/claude-sonnet-4.6" style="width:250px"></div>
                </div>
                <div class="form-row">
                    <div class="form-field"><label>Light Model</label><input id="s-model-light" value="google/gemini-2.5-flash" style="width:250px"></div>
                    <div class="form-field"><label>Fallback Model</label><input id="s-model-fallback" value="google/gemini-2.5-flash" style="width:250px"></div>
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
            </div>
            <div class="divider"></div>
            <div class="form-section">
                <h3>GitHub (optional)</h3>
                <div class="form-row"><div class="form-field"><label>GitHub Token</label><input id="s-gh-token" type="password" placeholder="ghp_..."></div></div>
                <div class="form-row"><div class="form-field"><label>GitHub Repo</label><input id="s-gh-repo" placeholder="owner/repo-name"></div></div>
            </div>
            <div class="divider"></div>
            <div class="form-row">
                <button class="btn btn-primary" id="btn-save-settings">Save Settings</button>
            </div>
            <div id="settings-status" style="margin-top:8px;font-size:13px;color:var(--accent);display:none"></div>
            <div class="divider"></div>
            <div class="form-section">
                <h3 style="color:var(--red)">Danger Zone</h3>
                <button class="btn btn-danger" id="btn-reset">Reset All Data</button>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    // Load current settings
    fetch('/api/settings').then(r => r.json()).then(s => {
        if (s.OUROBOROS_MODEL) document.getElementById('s-model').value = s.OUROBOROS_MODEL;
        if (s.OUROBOROS_MODEL_CODE) document.getElementById('s-model-code').value = s.OUROBOROS_MODEL_CODE;
        if (s.OUROBOROS_MODEL_LIGHT) document.getElementById('s-model-light').value = s.OUROBOROS_MODEL_LIGHT;
        if (s.OUROBOROS_MODEL_FALLBACK) document.getElementById('s-model-fallback').value = s.OUROBOROS_MODEL_FALLBACK;
        if (s.OUROBOROS_MAX_WORKERS) document.getElementById('s-workers').value = s.OUROBOROS_MAX_WORKERS;
        if (s.TOTAL_BUDGET) document.getElementById('s-budget').value = s.TOTAL_BUDGET;
        if (s.OUROBOROS_SOFT_TIMEOUT_SEC) document.getElementById('s-soft-timeout').value = s.OUROBOROS_SOFT_TIMEOUT_SEC;
        if (s.OUROBOROS_HARD_TIMEOUT_SEC) document.getElementById('s-hard-timeout').value = s.OUROBOROS_HARD_TIMEOUT_SEC;
        if (s.GITHUB_REPO) document.getElementById('s-gh-repo').value = s.GITHUB_REPO;
    }).catch(() => {});

    document.getElementById('btn-save-settings').addEventListener('click', async () => {
        const body = {
            OUROBOROS_MODEL: document.getElementById('s-model').value,
            OUROBOROS_MODEL_CODE: document.getElementById('s-model-code').value,
            OUROBOROS_MODEL_LIGHT: document.getElementById('s-model-light').value,
            OUROBOROS_MODEL_FALLBACK: document.getElementById('s-model-fallback').value,
            OUROBOROS_MAX_WORKERS: parseInt(document.getElementById('s-workers').value) || 5,
            TOTAL_BUDGET: parseFloat(document.getElementById('s-budget').value) || 10,
            OUROBOROS_SOFT_TIMEOUT_SEC: parseInt(document.getElementById('s-soft-timeout').value) || 600,
            OUROBOROS_HARD_TIMEOUT_SEC: parseInt(document.getElementById('s-hard-timeout').value) || 1800,
            GITHUB_REPO: document.getElementById('s-gh-repo').value,
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
            await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
            const status = document.getElementById('settings-status');
            status.textContent = 'Settings saved. Budget changes take effect immediately.';
            status.style.display = 'block';
            setTimeout(() => status.style.display = 'none', 4000);
        } catch (e) {
            alert('Failed to save: ' + e.message);
        }
    });

    document.getElementById('btn-reset').addEventListener('click', async () => {
        if (!confirm('This will delete all chat history and logs. Are you sure?')) return;
        try {
            const res = await fetch('/api/reset', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'ok') {
                alert('Deleted: ' + (data.deleted.join(', ') || 'nothing to delete'));
                location.reload();
            } else {
                alert('Error: ' + (data.error || 'unknown'));
            }
        } catch (e) {
            alert('Reset failed: ' + e.message);
        }
    });
}

// ---------------------------------------------------------------------------
// Logs Page
// ---------------------------------------------------------------------------
function initLogs() {
    const categories = {
        tools: { label: 'Tools', color: 'var(--blue)' },
        llm: { label: 'LLM', color: 'var(--accent)' },
        errors: { label: 'Errors', color: 'var(--red)' },
        tasks: { label: 'Tasks', color: 'var(--amber)' },
        system: { label: 'System', color: 'var(--text-muted)' },
        consciousness: { label: 'Consciousness', color: 'var(--accent)' },
    };

    const page = document.createElement('div');
    page.id = 'page-logs';
    page.className = 'page';
    page.innerHTML = `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            <h2>Logs</h2>
            <div class="spacer"></div>
            <button class="btn btn-default" id="btn-clear-logs">Clear</button>
        </div>
        <div class="logs-filters" id="log-filters"></div>
        <div id="log-entries"></div>
    `;
    document.getElementById('content').appendChild(page);

    const filtersDiv = document.getElementById('log-filters');
    Object.entries(categories).forEach(([key, cat]) => {
        const chip = document.createElement('button');
        chip.className = `filter-chip ${state.activeFilters[key] ? 'active' : ''}`;
        chip.textContent = cat.label;
        chip.addEventListener('click', () => {
            state.activeFilters[key] = !state.activeFilters[key];
            chip.classList.toggle('active');
        });
        filtersDiv.appendChild(chip);
    });

    const logEntries = document.getElementById('log-entries');
    const MAX_LOGS = 500;

    function categorizeEvent(evt) {
        const t = evt.type || evt.event || '';
        if (t.includes('error') || t.includes('crash') || t.includes('fail')) return 'errors';
        if (t.includes('llm') || t.includes('model')) return 'llm';
        if (t.includes('tool')) return 'tools';
        if (t.includes('task') || t.includes('evolution') || t.includes('review')) return 'tasks';
        if (t.includes('consciousness') || t.includes('bg_')) return 'consciousness';
        return 'system';
    }

    const LOG_PREVIEW_LEN = 200;

    function buildLogMessage(evt) {
        const t = evt.type || evt.event || '';
        let parts = [];
        if (evt.task_id) parts.push(`[${evt.task_id}]`);

        if (t === 'llm_round' || t === 'llm_usage') {
            if (evt.model) parts.push(evt.model);
            if (evt.round) parts.push(`r${evt.round}`);
            if (evt.prompt_tokens) parts.push(`${evt.prompt_tokens}→${evt.completion_tokens || 0}tok`);
            if (evt.cost_usd) parts.push(`$${Number(evt.cost_usd).toFixed(4)}`);
            else if (evt.cost) parts.push(`$${Number(evt.cost).toFixed(4)}`);
        } else if (t === 'task_eval' || t === 'task_done') {
            if (evt.task_type) parts.push(evt.task_type);
            if (evt.duration_sec) parts.push(`${evt.duration_sec.toFixed(1)}s`);
            if (evt.tool_calls != null) parts.push(`${evt.tool_calls} tools`);
            if (evt.cost_usd) parts.push(`$${Number(evt.cost_usd).toFixed(4)}`);
            if (evt.total_rounds) parts.push(`${evt.total_rounds} rounds`);
            if (evt.response_len) parts.push(`${evt.response_len} chars`);
        } else if (t === 'task_received') {
            const task = evt.task || {};
            if (task.type) parts.push(task.type);
            if (task.text) parts.push(task.text.slice(0, 100));
        } else if (t.includes('error') || t.includes('crash') || t.includes('fail')) {
            if (evt.error) parts.push(evt.error);
            if (evt.tool) parts.push(`tool=${evt.tool}`);
        } else {
            if (evt.model) parts.push(evt.model);
            if (evt.cost) parts.push(`$${Number(evt.cost).toFixed(4)}`);
            if (evt.cost_usd) parts.push(`$${Number(evt.cost_usd).toFixed(4)}`);
            if (evt.error) parts.push(evt.error);
        }
        if (evt.text) parts.push(evt.text.slice(0, 2000));
        return parts.join(' ');
    }

    function addLogEntry(evt) {
        const cat = categorizeEvent(evt);
        if (!state.activeFilters[cat]) return;

        const entry = document.createElement('div');
        entry.className = 'log-entry';
        const ts = (evt.ts || '').slice(11, 19);
        const type = evt.type || evt.event || 'unknown';
        let msg = buildLogMessage(evt);

        const isLong = msg.length > LOG_PREVIEW_LEN;
        const preview = isLong ? msg.slice(0, LOG_PREVIEW_LEN) + '...' : msg;

        entry.innerHTML = `
            <span class="log-ts">${ts}</span>
            <span class="log-type ${cat}">${type}</span>
            <span class="log-msg">${escapeHtml(preview)}</span>
        `;
        if (isLong) {
            entry.style.cursor = 'pointer';
            entry.title = 'Click to expand';
            let expanded = false;
            entry.addEventListener('click', () => {
                const msgEl = entry.querySelector('.log-msg');
                expanded = !expanded;
                msgEl.textContent = expanded ? msg : preview;
            });
        }
        logEntries.appendChild(entry);

        while (logEntries.children.length > MAX_LOGS) {
            logEntries.removeChild(logEntries.firstChild);
        }
        logEntries.scrollTop = logEntries.scrollHeight;
    }

    ws.on('log', (msg) => {
        if (msg.data) addLogEntry(msg.data);
    });

    document.getElementById('btn-clear-logs').addEventListener('click', () => {
        logEntries.innerHTML = '';
    });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Version display
// ---------------------------------------------------------------------------
async function loadVersion() {
    try {
        const resp = await fetch('/api/health');
        const data = await resp.json();
        document.getElementById('nav-version').textContent = `v${data.version || '?'}`;
        const dashTitle = document.getElementById('dash-title');
        if (dashTitle) dashTitle.textContent = `Ouroboros v${data.version}`;
    } catch {}
}

// ---------------------------------------------------------------------------
// Reconnect overlay
// ---------------------------------------------------------------------------
const overlay = document.createElement('div');
overlay.id = 'reconnect-overlay';
overlay.innerHTML = `
    <div class="spinner"></div>
    <div style="color:var(--text-secondary);font-size:14px">Reconnecting...</div>
`;
document.body.appendChild(overlay);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
initChat();
initDashboard();
initSettings();
initLogs();
loadVersion();
showPage('chat');
