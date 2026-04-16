function providerCard({ id, title, icon, hint, body, open = false }) {
    return `
        <details class="settings-provider-card" data-provider-card="${id}" ${open ? 'open' : ''}>
            <summary>
                <div class="settings-provider-title">
                    ${icon ? `<img src="${icon}" alt="" class="settings-provider-icon">` : ''}
                    <span>${title}</span>
                </div>
                <span class="settings-provider-hint">${hint || ''}</span>
            </summary>
            <div class="settings-provider-body">
                ${body}
            </div>
        </details>
    `;
}

function secretField({ id, settingKey, label, placeholder }) {
    return `
        <div class="form-field">
            <label>${label}</label>
            <div class="secret-input-row">
                <input id="${id}" data-secret-setting="${settingKey}" class="secret-input" type="password" placeholder="${placeholder}">
                <button type="button" class="settings-ghost-btn secret-toggle" data-target="${id}">Show</button>
                <button type="button" class="settings-ghost-btn secret-clear" data-target="${id}">Clear</button>
            </div>
        </div>
    `;
}

function modelCard({ title, copy, inputId, toggleId, defaultValue }) {
    return `
        <div class="settings-model-card">
            <div class="settings-model-header">
                <div>
                    <h4>${title}</h4>
                    <p>${copy}</p>
                </div>
                <label class="local-toggle"><input type="checkbox" id="${toggleId}"> Local</label>
            </div>
            <div class="model-picker" data-model-picker>
                <input
                    id="${inputId}"
                    value="${defaultValue}"
                    autocomplete="off"
                    spellcheck="false"
                >
                <div class="model-picker-results" hidden></div>
            </div>
        </div>
    `;
}

function effortField({ id, label, defaultValue }) {
    return `
        <div class="settings-effort-card">
            <label>${label}</label>
            <input id="${id}" type="hidden" value="${defaultValue}">
            <div class="settings-effort-group" data-effort-group data-effort-target="${id}">
                <button type="button" class="settings-effort-btn" data-effort-value="none">None</button>
                <button type="button" class="settings-effort-btn" data-effort-value="low">Low</button>
                <button type="button" class="settings-effort-btn" data-effort-value="medium">Medium</button>
                <button type="button" class="settings-effort-btn" data-effort-value="high">High</button>
            </div>
        </div>
    `;
}

export function renderSettingsPage() {
    return `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><circle cx="12" cy="12" r="3"/></svg>
            <h2>Settings</h2>
        </div>
        <div class="settings-shell">
            <div class="settings-tabs-bar">
                <div class="settings-tabs">
                    <button class="settings-tab active" data-settings-tab="providers">Providers</button>
                    <button class="settings-tab" data-settings-tab="models">Models</button>
                    <button class="settings-tab" data-settings-tab="behavior">Behavior</button>
                    <button class="settings-tab" data-settings-tab="integrations">Integrations</button>
                    <button class="settings-tab" data-settings-tab="advanced">Advanced</button>
                </div>
            </div>

            <div class="settings-scroll">
                <section class="settings-panel active" data-settings-panel="providers">
                    <div class="settings-section-copy">
                        Configure remote providers and the optional network gate. Secret fields now have explicit
                        <code>Clear</code> actions so masked values can be removed intentionally.
                    </div>
                    ${providerCard({
                        id: 'openrouter',
                        title: 'OpenRouter',
                        icon: '/static/providers/openrouter.ico',
                        hint: 'Default multi-model router',
                        open: true,
                        body: `<div class="form-row">${secretField({
                            id: 's-openrouter',
                            settingKey: 'OPENROUTER_API_KEY',
                            label: 'OpenRouter API Key',
                            placeholder: 'sk-or-...',
                        })}</div>`,
                    })}
                    ${providerCard({
                        id: 'openai',
                        title: 'OpenAI',
                        icon: '/static/providers/openai.svg',
                        hint: 'Official OpenAI API',
                        body: `
                            <div class="form-row">${secretField({
                                id: 's-openai',
                                settingKey: 'OPENAI_API_KEY',
                                label: 'OpenAI API Key',
                                placeholder: 'sk-...',
                            })}</div>
                            <div class="settings-inline-note">Use model values like <code>openai::gpt-5.4</code> in the Models tab to route models directly here. If OpenRouter is absent and the shipped defaults are still untouched, Ouroboros auto-remaps them to official OpenAI defaults.</div>
                        `,
                    })}
                    ${providerCard({
                        id: 'compatible',
                        title: 'OpenAI Compatible',
                        icon: '/static/providers/openai-compatible.svg',
                        hint: 'Custom OpenAI-style endpoint',
                        body: `
                            <div class="form-row">
                                ${secretField({
                                    id: 's-openai-compatible-key',
                                    settingKey: 'OPENAI_COMPATIBLE_API_KEY',
                                    label: 'API Key',
                                    placeholder: 'Compatible provider key',
                                })}
                                <div class="form-field">
                                    <label>Base URL</label>
                                    <input id="s-openai-compatible-base-url" placeholder="https://provider.example/v1">
                                </div>
                            </div>
                            <div class="settings-inline-note">Use this card for custom base URLs. Built-in web search only works with the official OpenAI Responses API, so keep <code>OPENAI_BASE_URL</code> empty when you want <code>web_search</code>.</div>
                        `,
                    })}
                    ${providerCard({
                        id: 'cloudru',
                        title: 'Cloud.ru Foundation Models',
                        icon: '/static/providers/cloudru.svg',
                        hint: 'Cloud.ru OpenAI-compatible runtime',
                        body: `
                            <div class="form-row">
                                ${secretField({
                                    id: 's-cloudru-key',
                                    settingKey: 'CLOUDRU_FOUNDATION_MODELS_API_KEY',
                                    label: 'API Key',
                                    placeholder: 'Cloud.ru Foundation Models API key',
                                })}
                                <div class="form-field">
                                    <label>Base URL</label>
                                    <input id="s-cloudru-base-url" placeholder="https://foundation-models.api.cloud.ru/v1">
                                </div>
                            </div>
                        `,
                    })}
                    ${providerCard({
                        id: 'anthropic',
                        title: 'Anthropic',
                        icon: '/static/providers/anthropic.png',
                        hint: 'Direct runtime plus Claude tooling',
                        body: `
                            <div class="form-row">${secretField({
                                id: 's-anthropic',
                                settingKey: 'ANTHROPIC_API_KEY',
                                label: 'Anthropic API Key',
                                placeholder: 'sk-ant-...',
                            })}</div>
                            <div class="settings-inline-note">Use model values like <code>anthropic::claude-sonnet-4-6</code> in the Models tab to route models directly through Anthropic. Claude tooling still reuses this key.</div>
                            <div class="settings-toolbar" id="settings-claude-code-panel" hidden>
                                <button type="button" class="settings-ghost-btn" id="btn-claude-code-install">Repair Runtime</button>
                                <span id="settings-claude-code-status" class="settings-inline-status">Checking Claude runtime...</span>
                            </div>
                            <div class="settings-inline-note" id="settings-claude-code-copy" hidden>Claude runtime powers delegated code editing and advisory review. It is managed automatically by the app.</div>
                        `,
                    })}
                    <div class="form-section compact">
                        <h3>Legacy Compatibility</h3>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Legacy OpenAI Base URL</label>
                                <input id="s-openai-base-url" placeholder="https://api.openai.com/v1 or compatible endpoint">
                            </div>
                        </div>
                        <div class="settings-inline-note">Backward-compatibility escape hatch for older installs. For new custom providers, use the dedicated <code>OpenAI Compatible</code> card instead.</div>
                    </div>
                    <div class="form-section compact">
                        <h3>Network Gate</h3>
                        <div class="form-row">
                            <div class="form-field">
                                <label class="local-toggle settings-lan-toggle">
                                    <input type="checkbox" id="s-lan-access"> Allow LAN Access
                                </label>
                                <div id="settings-lan-hint" class="form-hint settings-lan-hint"></div>
                            </div>
                        </div>
                        <div class="settings-inline-note">When enabled, the server binds to <code>0.0.0.0</code> so other devices on your local network can reach this UI. A restart is required after changing this setting.</div>
                        <div class="form-row">${secretField({
                            id: 's-network-password',
                            settingKey: 'OUROBOROS_NETWORK_PASSWORD',
                            label: 'Network Password (optional)',
                            placeholder: 'Leave blank to keep the network surface open',
                        })}</div>
                        <div class="settings-inline-note">Adds a password wall only for non-localhost app and API access. Leave it blank if you use Ouroboros only on this machine or inside a trusted private network. External binds still start without it, but startup logs a warning.</div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="models">
                    <div class="form-section">
                        <h3>Model Routing</h3>
                        <div class="settings-section-copy">
                            These fields are cloud model IDs. Enable <code>Local</code> to route that model
                            through the GGUF server configured in Advanced.
                        </div>
                        <div class="settings-toolbar">
                            <button type="button" class="settings-ghost-btn" id="btn-refresh-model-catalog">Refresh Model Catalog</button>
                            <span id="settings-model-catalog-status" class="settings-inline-status">Model catalog is optional and failure-tolerant.</span>
                        </div>
                        <div class="settings-model-grid">
                            ${modelCard({ title: 'Main', copy: 'Primary reasoning model.', inputId: 's-model', toggleId: 's-local-main', defaultValue: 'anthropic/claude-opus-4.7' })}
                            ${modelCard({ title: 'Code', copy: 'Tool-heavy coding model.', inputId: 's-model-code', toggleId: 's-local-code', defaultValue: 'anthropic/claude-opus-4.7' })}
                            ${modelCard({ title: 'Light', copy: 'Fast summaries and lightweight tasks.', inputId: 's-model-light', toggleId: 's-local-light', defaultValue: 'anthropic/claude-sonnet-4.6' })}
                            ${modelCard({ title: 'Fallback', copy: 'Resilience and degraded path.', inputId: 's-model-fallback', toggleId: 's-local-fallback', defaultValue: 'anthropic/claude-sonnet-4.6' })}
                        </div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Claude Code Model</label>
                                <input id="s-claude-code-model" value="claude-opus-4-7[1m]" placeholder="sonnet, opus, claude-opus-4-7[1m], or full name">
                                <div class="settings-inline-note">Anthropic model for <code>claude_code_edit</code> and <code>advisory_pre_review</code> tools. Requires Anthropic key in Providers.</div>
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Review Models</h3>
                        <div class="settings-section-copy">Models used by the pre-commit review gate. Runs automatically on every <code>repo_commit</code>.</div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Pre-commit Review Models</label>
                                <input id="s-review-models" placeholder="model1,model2,model3">
                                <div class="settings-inline-note">Comma-separated review models (triad). In OpenAI-only or Anthropic-only direct-provider mode, the list is auto-normalized to [main, light, light] (3 slots, 2 unique) so both the commit triad and plan_task (which requires >=2 distinct models for majority-vote) work out of the box. OpenAI-compatible and Cloud.ru setups are not auto-normalized and must configure the list explicitly.</div>
                            </div>
                        </div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label>Scope Review Model</label>
                                <input id="s-scope-review-model" placeholder="anthropic/claude-opus-4.6">
                                <div class="settings-inline-note">Single model for the blocking scope reviewer. Runs in parallel with the triad diff review.</div>
                            </div>
                            <div class="form-field">
                                <label>Web Search Model</label>
                                <input id="s-websearch-model" placeholder="gpt-5.2">
                                <div class="settings-inline-note">OpenAI model for <code>web_search</code>. Requires <code>OPENAI_API_KEY</code> and an empty Legacy Base URL.</div>
                            </div>
                        </div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="behavior">
                    <div class="form-section">
                        <h3>Reasoning Effort</h3>
                        <div class="settings-section-copy">Controls how deeply the model thinks per task type. Higher effort = slower but more thorough.</div>
                        <div class="settings-effort-grid">
                            ${effortField({ id: 's-effort-task', label: 'Task / Chat', defaultValue: 'medium' })}
                            ${effortField({ id: 's-effort-evolution', label: 'Evolution', defaultValue: 'high' })}
                            ${effortField({ id: 's-effort-review', label: 'Review', defaultValue: 'medium' })}
                            ${effortField({ id: 's-effort-scope-review', label: 'Scope Review', defaultValue: 'high' })}
                            ${effortField({ id: 's-effort-consciousness', label: 'Consciousness', defaultValue: 'low' })}
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Review Enforcement</h3>
                        <div class="settings-section-copy"><code>Advisory</code> keeps review visible but non-blocking. <code>Blocking</code> stops commits when critical findings remain unresolved.</div>
                        <div class="settings-effort-card">
                            <label>Enforcement Mode</label>
                            <input id="s-review-enforcement" type="hidden" value="advisory">
                            <div class="settings-effort-group" data-effort-group data-enforcement-group data-effort-target="s-review-enforcement">
                                <button type="button" class="settings-effort-btn" data-effort-value="advisory">Advisory</button>
                                <button type="button" class="settings-effort-btn" data-effort-value="blocking">Blocking</button>
                            </div>
                        </div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="integrations">
                    <div class="form-section">
                        <h3>Telegram Bridge</h3>
                        <div class="form-row">${secretField({
                            id: 's-telegram-token',
                            settingKey: 'TELEGRAM_BOT_TOKEN',
                            label: 'Bot Token',
                            placeholder: '123456:ABCDEF...',
                        })}</div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Primary Chat ID (optional)</label>
                                <input id="s-telegram-chat-id" placeholder="123456789">
                            </div>
                        </div>
                        <div class="settings-inline-note">If no primary chat is pinned, the bridge binds to the first active Telegram chat and keeps replies attached there.</div>
                    </div>

                    <div class="form-section">
                        <h3>GitHub</h3>
                        <div class="form-row">${secretField({
                            id: 's-gh-token',
                            settingKey: 'GITHUB_TOKEN',
                            label: 'GitHub Token',
                            placeholder: 'ghp_...',
                        })}</div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>GitHub Repo</label>
                                <input id="s-gh-repo" placeholder="owner/repo-name">
                            </div>
                        </div>
                        <div class="settings-inline-note">Only needed for in-app remote sync features. Safe to leave empty if you work locally.</div>
                    </div>
                    <div class="form-section">
                        <h3>A2A Protocol</h3>
                        <div class="settings-section-copy">Agent-to-Agent communication server. Disabled by default. Requires restart to toggle.</div>
                        <div class="form-row">
                            <div class="form-field checkbox-field">
                                <label for="s-a2a-enabled">Enable A2A Server</label>
                                <input type="checkbox" id="s-a2a-enabled">
                            </div>
                        </div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label for="s-a2a-host">A2A Host</label>
                                <input type="text" id="s-a2a-host" placeholder="127.0.0.1">
                            </div>
                            <div class="form-field">
                                <label for="s-a2a-port">A2A Port</label>
                                <input type="number" id="s-a2a-port" placeholder="18800">
                            </div>
                        </div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label for="s-a2a-agent-name">Agent Name (override)</label>
                                <input type="text" id="s-a2a-agent-name" placeholder="Auto-detected from identity.md">
                            </div>
                            <div class="form-field">
                                <label for="s-a2a-agent-description">Agent Description (override)</label>
                                <input type="text" id="s-a2a-agent-description" placeholder="Auto-detected from identity.md">
                            </div>
                        </div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label for="s-a2a-max-concurrent">Max Concurrent Tasks</label>
                                <input type="number" id="s-a2a-max-concurrent" placeholder="3">
                            </div>
                            <div class="form-field">
                                <label for="s-a2a-ttl-hours">Task TTL (hours)</label>
                                <input type="number" id="s-a2a-ttl-hours" placeholder="24">
                            </div>
                        </div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="advanced">
                    <div class="form-section">
                        <h3>Local Model Runtime</h3>
                        <div class="settings-section-copy">Only fill this in when you want Ouroboros to start and route to a GGUF model on this machine.</div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label>Model Source</label>
                                <input id="s-local-source" placeholder="bartowski/Llama-3.3-70B-Instruct-GGUF or /path/to/model.gguf">
                            </div>
                            <div class="form-field">
                                <label>GGUF Filename (for HF repos)</label>
                                <input id="s-local-filename" placeholder="Llama-3.3-70B-Instruct-Q4_K_M.gguf">
                            </div>
                        </div>
                        <div class="form-grid four">
                            <div class="form-field">
                                <label>Port</label>
                                <input id="s-local-port" type="number" value="8766">
                            </div>
                            <div class="form-field">
                                <label>GPU Layers (-1 = all)</label>
                                <input id="s-local-gpu-layers" type="number" value="-1">
                            </div>
                            <div class="form-field">
                                <label>Context Length</label>
                                <input id="s-local-ctx" type="number" value="16384">
                            </div>
                            <div class="form-field">
                                <label>Chat Format</label>
                                <input id="s-local-chat-format" placeholder="auto-detect">
                            </div>
                        </div>
                        <div class="settings-toolbar">
                            <button class="btn btn-primary" id="btn-local-start">Start</button>
                            <button class="btn btn-primary" id="btn-local-stop">Stop</button>
                            <button class="btn btn-primary" id="btn-local-test">Test Tool Calling</button>
                        </div>
                        <div id="local-model-status" class="settings-inline-status">Status: Offline</div>
                        <div id="local-model-progress-wrap" class="local-model-progress-wrap local-model-hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                            <div id="local-model-progress-bar" class="local-model-progress-bar"></div>
                        </div>
                        <button class="btn btn-secondary local-model-install-btn local-model-hidden" id="btn-local-install-runtime">Install Local Runtime</button>
                        <div id="local-model-test-result" class="settings-test-result"></div>
                    </div>

                    <div class="form-section">
                        <h3>Runtime Limits</h3>
                        <div class="settings-section-copy">Workers control parallel task capacity. Timeout values are safety rails for long or stuck tasks and tools.</div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label>Max Workers</label>
                                <input id="s-workers" type="number" min="1" max="10" value="5">
                            </div>
                            <div class="form-field">
                                <label>Soft Timeout (s)</label>
                                <input id="s-soft-timeout" type="number" value="600">
                            </div>
                            <div class="form-field">
                                <label>Hard Timeout (s)</label>
                                <input id="s-hard-timeout" type="number" value="1800">
                            </div>
                            <div class="form-field">
                                <label>Tool Timeout (s)</label>
                                <input id="s-tool-timeout" type="number" value="120">
                            </div>
                        </div>
                    </div>

                    <div class="form-section danger">
                        <h3>Danger Zone</h3>
                        <div class="settings-inline-note">Reset still uses the current restart-based flow. This clears runtime data but keeps the repo.</div>
                        <button class="btn btn-danger" id="btn-reset">Reset All Data</button>
                    </div>
                </section>
            </div>

            <div class="settings-footer">
                <button class="btn btn-save" id="btn-save-settings">Save Settings</button>
                <div id="settings-status" class="settings-inline-status"></div>
            </div>
        </div>
    `;
}

export function bindSettingsTabs(root) {
    const tabs = Array.from(root.querySelectorAll('.settings-tab'));
    const panels = Array.from(root.querySelectorAll('.settings-panel'));
    const scrollRoot = root.querySelector('.settings-scroll');

    function activate(tabName) {
        tabs.forEach((button) => {
            button.classList.toggle('active', button.dataset.settingsTab === tabName);
        });
        panels.forEach((panel) => {
            panel.classList.toggle('active', panel.dataset.settingsPanel === tabName);
        });
        if (scrollRoot) scrollRoot.scrollTop = 0;
    }

    tabs.forEach((button) => {
        button.addEventListener('click', () => activate(button.dataset.settingsTab));
    });
}

export function bindSecretInputs(root) {
    root.querySelectorAll('.secret-input').forEach((input) => {
        input.addEventListener('focus', () => {
            if (input.value.includes('...')) input.value = '';
        });
        input.addEventListener('input', () => {
            if (input.value.trim()) delete input.dataset.forceClear;
        });
    });

    root.querySelectorAll('.secret-toggle').forEach((button) => {
        button.addEventListener('click', () => {
            const target = root.querySelector(`#${button.dataset.target}`);
            if (!target) return;
            const nextType = target.type === 'password' ? 'text' : 'password';
            target.type = nextType;
            button.textContent = nextType === 'password' ? 'Show' : 'Hide';
        });
    });

    root.querySelectorAll('.secret-clear').forEach((button) => {
        button.addEventListener('click', () => {
            const target = root.querySelector(`#${button.dataset.target}`);
            if (!target) return;
            target.value = '';
            target.type = 'password';
            target.dataset.forceClear = '1';
            const toggle = root.querySelector(`.secret-toggle[data-target="${button.dataset.target}"]`);
            if (toggle) toggle.textContent = 'Show';
        });
    });
}
