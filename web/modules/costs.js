import { formatUsd2 } from './utils.js';
import { apiFetch } from './api_client.js';

const COST_BUDGET_INPUTS = {
    TOTAL_BUDGET: 's-budget',
    OUROBOROS_PER_TASK_COST_USD: 's-per-task-cost',
};

function readPositiveBudget(id) {
    const input = document.getElementById(id);
    const raw = String(input?.value || '').trim();
    const value = Number(raw);
    const min = Number(input?.min || 0.01);
    return Number.isFinite(value) && value >= min ? value : null;
}

export function initCosts({ state, mount }) {
    const page = document.createElement('div');
    page.id = 'page-costs';
    page.className = 'settings-embedded-content settings-costs-panel';
    page.innerHTML = `
        <div class="costs-scroll">
            <div class="costs-budget-card">
                <div class="costs-budget-head">
                    <h3 class="costs-budget-title">Budget</h3>
                    <button class="btn btn-default btn-sm costs-budget-refresh" id="btn-refresh-costs">Refresh</button>
                </div>
                <div class="costs-budget-fields">
                    <div class="form-field">
                        <label>Total Budget ($)</label>
                        <input id="s-budget" type="number" value="10">
                    </div>
                    <div class="form-field">
                        <label>Per-task Cost Cap ($)</label>
                        <input id="s-per-task-cost" type="number" value="20">
                        <div class="settings-inline-note">Soft threshold only. When a task crosses it, Ouroboros is asked to wrap up rather than being hard-killed.</div>
                    </div>
                </div>
                <button class="btn btn-save costs-budget-save" id="btn-save-budget">Save Budget</button>
                <div id="budget-save-status" class="settings-inline-status"></div>
            </div>
            <div class="costs-stats-grid">
                <div class="stat-card"><div class="label">Total Spent</div><div class="value" id="cost-total">$0.00</div></div>
                <div class="stat-card"><div class="label">Total Calls</div><div class="value" id="cost-calls">0</div></div>
                <div class="stat-card"><div class="label">Top Model</div><div class="value cost-top-model" id="cost-top-model">-</div></div>
            </div>
            <div class="costs-tables-grid">
                <div>
                    <h3 class="costs-table-label">By Model</h3>
                    <table class="cost-table" id="cost-by-model"><thead><tr><th>Model</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By API Key</h3>
                    <table class="cost-table" id="cost-by-key"><thead><tr><th>Key</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By Model Category</h3>
                    <table class="cost-table" id="cost-by-model-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
                <div>
                    <h3 class="costs-table-label">By Task Category</h3>
                    <table class="cost-table" id="cost-by-task-cat"><thead><tr><th>Category</th><th>Calls</th><th>Cost</th><th></th></tr></thead><tbody></tbody></table>
                </div>
            </div>
        </div>
    `;
    mount.appendChild(page);

    function renderBreakdownTable(tableId, data, totalCost) {
        const tbody = document.querySelector('#' + tableId + ' tbody');
        tbody.innerHTML = '';
        const cell = (className, text, attrs = {}) => {
            const td = document.createElement('td');
            td.className = className;
            td.textContent = text;
            Object.entries(attrs).forEach(([key, value]) => td.setAttribute(key, value));
            return td;
        };
        for (const [name, info] of Object.entries(data)) {
            const pct = totalCost > 0 ? (info.cost / totalCost * 100) : 0;
            const tr = document.createElement('tr');
            const bar = document.createElement('progress');
            bar.className = 'cost-bar';
            bar.max = 100;
            bar.value = Math.min(100, pct);
            const tdBar = document.createElement('td');
            tdBar.className = 'cost-bar-cell';
            tdBar.appendChild(bar);
            tr.append(
                cell('cost-cell-name', name, { title: name }),
                cell('cost-cell-right', info.calls),
                cell('cost-cell-right', formatUsd2(info.cost)),
                tdBar,
            );
            tbody.appendChild(tr);
        }
        if (Object.keys(data).length === 0) {
            const tr = document.createElement('tr');
            tr.appendChild(cell('cost-empty-cell', 'No data', { colspan: '4' }));
            tbody.appendChild(tr);
        }
    }

    async function loadCosts() {
        try {
            const resp = await apiFetch('/api/cost-breakdown');
            const d = await resp.json();
            document.getElementById('cost-total').textContent = formatUsd2(d.total_cost || 0);
            document.getElementById('cost-calls').textContent = d.total_calls || 0;
            const models = Object.entries(d.by_model || {});
            document.getElementById('cost-top-model').textContent = models.length > 0 ? models[0][0] : '-';
            renderBreakdownTable('cost-by-model', d.by_model || {}, d.total_cost);
            renderBreakdownTable('cost-by-key', d.by_api_key || {}, d.total_cost);
            renderBreakdownTable('cost-by-model-cat', d.by_model_category || {}, d.total_cost);
            renderBreakdownTable('cost-by-task-cat', d.by_task_category || {}, d.total_cost);
        } catch {}
    }

    async function loadBudget() {
        try {
            const resp = await apiFetch('/api/settings', { cache: 'no-store' });
            const s = await resp.json().catch(() => ({}));
            const fields = s?._meta?.setup_contract?.budgetFields || [];
            fields.forEach((field) => {
                const input = document.getElementById(COST_BUDGET_INPUTS[field.settingKey]);
                if (!input) return;
                input.min = field.min || '0.01';
                input.step = field.step || 'any';
                if (field.default != null && !String(input.value || '').trim()) {
                    input.value = field.default;
                }
            });
            if (s.TOTAL_BUDGET != null) document.getElementById('s-budget').value = s.TOTAL_BUDGET;
            if (s.OUROBOROS_PER_TASK_COST_USD != null) document.getElementById('s-per-task-cost').value = s.OUROBOROS_PER_TASK_COST_USD;
        } catch {}
    }

    document.getElementById('btn-refresh-costs').addEventListener('click', loadCosts);

    document.getElementById('btn-save-budget').addEventListener('click', async () => {
        const statusEl = document.getElementById('budget-save-status');
        const budget = readPositiveBudget('s-budget');
        const perTask = readPositiveBudget('s-per-task-cost');
        if (budget === null || perTask === null) {
            statusEl.textContent = 'Budget values must be at least 0.01.';
            return;
        }
        try {
            const resp = await apiFetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ TOTAL_BUDGET: budget, OUROBOROS_PER_TASK_COST_USD: perTask }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            let msg;
            if (data.no_changes) {
                msg = 'No changes.';
            } else if (data.restart_required) {
                msg = 'Saved. Restart required.';
            } else if (data.immediate_changed && data.next_task_changed) {
                msg = 'Saved. Some changes took effect immediately; others apply on the next task.';
            } else if (data.immediate_changed) {
                msg = 'Saved. Took effect immediately.';
            } else {
                msg = 'Saved. Applies on the next task.';
            }
            if (data.warnings && data.warnings.length) msg += ' ⚠️ ' + data.warnings.join(' | ');
            statusEl.textContent = msg;
            window.dispatchEvent(new CustomEvent('ouro:settings-updated', { detail: { reason: 'budget saved', source: 'costs' } }));
        } catch (e) {
            statusEl.textContent = 'Error: ' + e.message;
        }
        setTimeout(() => { statusEl.textContent = ''; }, 4000);
    });

    function refreshCostsPanel() {
        loadCosts();
        loadBudget();
    }

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab === 'costs' && state.activePage === 'dashboard') refreshCostsPanel();
    });
    window.addEventListener('ouro:settings-updated', (event) => {
        if (event.detail?.source === 'costs') return;
        refreshCostsPanel();
    });
}
