(() => {
    // Self-contained IIFE mirror of utils.escapeHtmlAttr; SSOT drift is tested.
    function escapeHtml(value) {
        return String(value ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/`/g, '&#96;');
    }

    const bootstrap = window.__OURO_ONBOARDING_BOOTSTRAP__ || {};
    const SETUP_CONTRACT = bootstrap.contract || {};
    const HOST_MODE = bootstrap.hostMode || 'desktop';
    const LOCAL_RUNTIME_CONTROLS = Boolean(bootstrap.supportsLocalRuntimeControls);
    const STEP_ORDER = bootstrap.stepOrder || (SETUP_CONTRACT.steps || []).map((step) => step.id);
    const STEP_META = Object.assign(
        Object.fromEntries((SETUP_CONTRACT.steps || []).map((step) => [step.id, step])),
        {
            providers: {
                title: 'Добавьте доступ',
                railCopy: 'Ключи + локальная',
                copy: 'Заполните хотя бы один удалённый ключ или источник локальной модели. Следующий шаг адаптируется к тому, что вы настроили здесь.',
                footer: 'Вставляйте только то, что у вас уже есть. OpenRouter, прямые ключи провайдеров и необязательная локальная модель могут сосуществовать.',
            },
            models: {
                title: 'Выберите модели',
                railCopy: '4 слота моделей',
                copy: 'Просмотрите видимые настройки моделей по умолчанию, полученные из вашей текущей конфигурации, затем измените всё, что нужно, перед запуском.',
                footer: 'Значения вида openai/... или anthropic/... остаются в стиле роутера. Прямые значения используют openai::... и anthropic::....',
            },
            review_mode: {
                title: 'Выберите режим проверки',
                railCopy: 'Рекомендательный / Блокирующий',
                copy: 'Определите строгость проверки перед коммитом до того, как Ouroboros начнёт самомодификацию.',
                footer: 'Выберите режим проверки и начальный режим среды выполнения до запуска Ouroboros.',
            },
            budget: {
                title: 'Установите бюджет',
                railCopy: 'Ограничения сессии',
                copy: 'Бюджет — отдельный шаг, потому что он напрямую определяет, насколько далеко Ouroboros может зайти за одну сессию и в одной задаче.',
                footer: 'Общий бюджет — глобальный. Лимит затрат на задачу — мягкое напоминание, а не жёсткий выключатель.',
            },
            summary: {
                title: 'Проверьте перед запуском',
                railCopy: 'Финальная проверка',
                copy: 'Проверьте итоговую картину по провайдерам, моделям, проверке и бюджету. Ouroboros сохранит эти значения перед запуском.',
                footer: 'Те же параметры останутся доступными для редактирования в Настройках.',
            },
        },
    );
    const PROVIDER_FIELDS = SETUP_CONTRACT.providerFields || [];
    const PROVIDER_PROFILES = SETUP_CONTRACT.providerProfiles || {};
    const MODEL_SLOTS = SETUP_CONTRACT.modelSlots || [];
    const REVIEW_MODES = SETUP_CONTRACT.reviewModes || [];
    const RUNTIME_MODES = SETUP_CONTRACT.runtimeModes || [];
    const LOCAL_ROUTING_MODES = SETUP_CONTRACT.localRoutingModes || [];
    const BUDGET_FIELDS = SETUP_CONTRACT.budgetFields || [];
    const LOCAL_FIELDS = [
        ['local-source', 'localSource', 'Источник модели', 'Qwen/Qwen2.5-7B-Instruct-GGUF или /абсолютный/путь/model.gguf', 'Используйте либо ID репозитория HuggingFace, либо абсолютный путь к GGUF-файлу.', 'field field-full'],
        ['local-filename', 'localFilename', 'Имя GGUF-файла', 'qwen2.5-7b-instruct-q3_k_m.gguf', 'Требуется только для ID репозиториев HuggingFace. Оставьте пустым, если указан прямой путь.', 'field field-full'],
        ['local-context', 'localContextLength', 'Длина контекста', '', '', 'field', 'number', '2048', '1024'],
        ['local-gpu-layers', 'localGpuLayers', 'GPU-слои', '', '', 'field', 'number', '', '1'],
        ['local-chat-format', 'localChatFormat', 'Формат чата', 'Оставьте пустым для автоопределения', '', 'field field-full'],
    ];
    const MODEL_DEFAULTS = bootstrap.modelDefaults || {};
    const LOCAL_PRESETS = bootstrap.localPresets || {};
    const MODEL_SUGGESTIONS = bootstrap.modelSuggestions || [];
    const INITIAL_STATE = bootstrap.initialState || {};
    const root = document.getElementById('root');

    const state = Object.assign({
        currentStep: STEP_ORDER[0],
        error: '',
        saving: false,
        modelsDirty: false,
        localSourceOpen: Boolean(INITIAL_STATE.localSource),
        localStatusText: 'Статус: Офлайн',
        localStatusTone: 'muted',
        localTestResult: '',
        localTestTone: 'muted',
        localRuntimeReady: false,
        claudeCliInstalled: false,
        claudeCliBusy: false,
        claudeCliStatus: '',
        claudeCliStatusText: 'Проверка среды Claude...',
        claudeCliTone: 'muted',
        claudeCliError: '',
        claudeCliDismissed: false,
    }, INITIAL_STATE);

    let localStatusPollStarted = false;
    let claudeCliPollStarted = false;

    function trim(value) {
        return String(value || '').trim();
    }

    function formatUsd(value) {
        const num = Number(value);
        return Number.isFinite(num) ? `$${num.toFixed(2)}` : '$0.00';
    }

    function hasLocalModel() {
        return trim(state.localSource).length > 0;
    }

    function hasAnthropicKeyConfigured() {
        return trim(state.anthropicKey).length >= 10;
    }

    function shouldShowClaudeCliCta() {
        return hasAnthropicKeyConfigured() && !state.claudeCliDismissed;
    }

        function isLocalFilesystemSource(value) {
            const text = trim(value);
            return text.startsWith('/') || text.startsWith('~');
        }

        function optionByValue(items, value) {
            return (items || []).find((item) => item.value === value) || {};
        }

        function detectProviderProfile() {
            const configured = Object.fromEntries(PROVIDER_FIELDS.map((field) => [
                field.settingKey,
                trim(state[field.stateKey]).length >= 10,
            ]));
            const hasOpenrouter = configured.OPENROUTER_API_KEY;
            const direct = [
                ['OPENAI_API_KEY', 'openai'],
                ['CLOUDRU_FOUNDATION_MODELS_API_KEY', 'cloudru'],
                ['ANTHROPIC_API_KEY', 'anthropic'],
            ].filter(([settingKey]) => configured[settingKey]);
            if (hasOpenrouter) return 'openrouter';
            if (direct.length > 1) return 'direct-multi';
            if (direct.length === 1) return direct[0][1];
            if (hasLocalModel()) return 'local';
            return 'openrouter';
        }

    function activeProviderProfile() {
        const profile = detectProviderProfile();
        state.providerProfile = profile;
        return profile;
    }

    function profileLabel(profile) {
        return PROVIDER_PROFILES[profile]?.label || PROVIDER_PROFILES.openrouter?.label || 'OpenRouter';
    }

    function reviewLabel(mode) {
        return optionByValue(REVIEW_MODES, mode).label || 'Рекомендательный';
    }

    function runtimeModeLabel(mode) {
        return optionByValue(RUNTIME_MODES, mode).label || 'Advanced';
    }

    function localRoutingLabel(mode) {
        return optionByValue(LOCAL_ROUTING_MODES, mode).label || 'Только облачные модели';
    }

    function nextButtonShouldBeDisabled() {
        if (state.saving) return true;
        if (state.currentStep === 'summary') return false;
        return Boolean(validateCurrentStep());
    }

    function syncCurrentStepActionState() {
        const next = document.getElementById('next-btn');
        if (next) next.disabled = nextButtonShouldBeDisabled();
    }

    function markStepEdited() {
        state.error = '';
        syncCurrentStepActionState();
    }

    function applyPresetSelection(presetId) {
        state.localPreset = presetId;
        state.localSourceOpen = Boolean(presetId);
        if (!presetId) {
            state.localSource = '';
            state.localFilename = '';
            state.localContextLength = 16384;
            state.localGpuLayers = -1;
            state.localChatFormat = '';
            state.localRoutingMode = 'cloud';
            return;
        }
        if (presetId === 'custom') {
            if (!trim(state.localSource)) {
                state.localSource = '';
                state.localFilename = '';
            }
            return;
        }
        const preset = LOCAL_PRESETS[presetId];
        if (!preset) return;
        state.localSource = preset.source;
        state.localFilename = preset.filename;
        state.localContextLength = preset.contextLength;
        state.localChatFormat = preset.chatFormat || '';
        if (activeProviderProfile() === 'local') {
            state.localRoutingMode = 'all';
        } else if (state.localRoutingMode === 'cloud') {
            state.localRoutingMode = 'fallback';
        }
    }

    function detectLocalPresetSelection() {
        const source = trim(state.localSource);
        const filename = trim(state.localFilename);
        if (!source && !filename) return '';
        for (const [presetId, preset] of Object.entries(LOCAL_PRESETS)) {
            if (source === trim(preset.source) && filename === trim(preset.filename)) {
                return presetId;
            }
        }
        return 'custom';
    }

    function applyModelDefaults(force) {
        if (state.modelsDirty && !force) return;
        const defaults = MODEL_DEFAULTS[activeProviderProfile()] || MODEL_DEFAULTS.openrouter || {};
        state.mainModel = defaults.main || '';
        state.codeModel = defaults.code || '';
        state.lightModel = defaults.light || '';
        state.fallbackModel = defaults.fallback || '';
        state.modelsDirty = false;
    }

    function validateProvidersStep() {
        const keyValues = PROVIDER_FIELDS.map((field) => [field, trim(state[field.stateKey])]);
        const localSource = trim(state.localSource);
        const localFilename = trim(state.localFilename);
        const shortKey = keyValues.find(([, value]) => value && value.length < 10);
        if (shortKey) return `Ключ ${shortKey[0].label.replace(' API Key', '')} выглядит слишком коротким.`;
        const hasRemote = keyValues.some(([, value]) => value);
        if (!hasRemote && !localSource) {
            return 'Введите хотя бы один удалённый ключ или источник локальной модели перед продолжением.';
        }
        if (localSource && !hasRemote && trim(state.localRoutingMode) === 'cloud') {
            return 'Только-локальные конфигурации должны маршрутизировать хотя бы одну модель на локальную среду.';
        }
        if (localSource && localSource.includes('/') && !isLocalFilesystemSource(localSource) && !localFilename) {
            return 'Для локальных источников HuggingFace требуется имя файла GGUF.';
        }
        if (localSource && (!Number.isInteger(Number(state.localContextLength)) || Number(state.localContextLength) <= 0)) {
            return 'Длина контекста должна быть положительным целым числом.';
        }
        if (localSource && !Number.isInteger(Number(state.localGpuLayers))) {
            return 'Количество слоёв GPU должно быть целым числом.';
        }
        return '';
    }

    function validateModelsStep() {
        if (!trim(state.mainModel) || !trim(state.codeModel) || !trim(state.lightModel) || !trim(state.fallbackModel)) {
            return 'Подтвердите все четыре модели перед запуском Ouroboros.';
        }
        return '';
    }

    function validateReviewStep() {
        if (!['advisory', 'blocking'].includes(trim(state.reviewEnforcement))) {
            return 'Выберите рекомендательный или блокирующий режим проверки.';
        }
        return '';
    }

    function validateBudgetStep() {
        const totalBudget = Number(state.totalBudget);
        const perTaskCostUsd = Number(state.perTaskCostUsd);
        if (!Number.isFinite(totalBudget) || totalBudget <= 0) {
            return 'Общий бюджет должен быть больше нуля.';
        }
        if (!Number.isFinite(perTaskCostUsd) || perTaskCostUsd <= 0) {
            return 'Мягкий порог затрат на задачу должен быть больше нуля.';
        }
        return '';
    }

    function validateCurrentStep() {
        if (state.currentStep === 'providers') return validateProvidersStep();
        if (state.currentStep === 'models') return validateModelsStep();
        if (state.currentStep === 'review_mode') return validateReviewStep();
        if (state.currentStep === 'budget') return validateBudgetStep();
        return '';
    }

    function nextStep() {
        const error = validateCurrentStep();
        state.error = error;
        if (error) {
            render();
            return;
        }
        if (state.currentStep === 'providers') applyModelDefaults(false);
        const index = STEP_ORDER.indexOf(state.currentStep);
        if (index >= 0 && index < STEP_ORDER.length - 1) {
            state.currentStep = STEP_ORDER[index + 1];
        }
        state.error = '';
        render();
    }

    function previousStep() {
        const index = STEP_ORDER.indexOf(state.currentStep);
        if (index > 0) state.currentStep = STEP_ORDER[index - 1];
        state.error = '';
        render();
    }

    async function apiRequest(url, init = {}) {
        const response = await fetch(url, init);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `HTTP ${response.status}`);
        }
        return data;
    }

    function applyClaudeCliStatus(payload = {}) {
        const ready = Boolean(payload.ready);
        const installed = Boolean(payload.installed);
        const busy = Boolean(payload.busy);
        const errorText = trim(payload.error);
        const message = trim(payload.message)
            || (ready ? 'Среда Claude готова.' : (installed ? 'Среда Claude доступна, но не готова.' : 'Среда Claude недоступна.'));
        state.claudeCliInstalled = installed || ready;
        state.claudeCliBusy = busy;
        state.claudeCliStatus = trim(payload.status) || (ready ? 'ready' : (installed ? 'installed' : 'missing'));
        state.claudeCliError = errorText;
        state.claudeCliTone = ready ? 'ok' : (errorText ? 'error' : (installed ? 'muted' : 'error'));
        state.claudeCliStatusText = message;
        renderClaudeCliStatus();
    }

    async function claudeCliRequestStatus() {
        if (HOST_MODE === 'web') {
            return apiRequest('/api/claude-code/status', { cache: 'no-store' });
        }
        if (!window.pywebview?.api?.claude_code_status) {
            throw new Error('Desktop Claude Code bridge is unavailable.');
        }
        return window.pywebview.api.claude_code_status();
    }

    async function claudeCliStartInstall() {
        if (HOST_MODE === 'web') {
            return apiRequest('/api/claude-code/install', { method: 'POST' });
        }
        if (!window.pywebview?.api?.install_claude_code) {
            throw new Error('Desktop Claude Code install bridge is unavailable.');
        }
        return window.pywebview.api.install_claude_code();
    }

    async function updateClaudeCliStatus() {
        if (!shouldShowClaudeCliCta()) return;
        try {
            applyClaudeCliStatus(await claudeCliRequestStatus());
        } catch (error) {
            state.claudeCliInstalled = false;
            state.claudeCliBusy = false;
            state.claudeCliStatus = 'error';
            state.claudeCliError = String(error?.message || error || '');
            state.claudeCliTone = 'error';
            state.claudeCliStatusText = `Ошибка проверки среды Claude: ${state.claudeCliError}`;
            renderClaudeCliStatus();
        }
    }

    function startClaudeCliStatusPolling() {
        if (claudeCliPollStarted) return;
        claudeCliPollStarted = true;
        updateClaudeCliStatus();
        setInterval(() => {
            if (shouldShowClaudeCliCta()) updateClaudeCliStatus();
        }, 3000);
    }

    function syncClaudeCliVisibility() {
        const card = document.getElementById('wizard-claude-card');
        if (card) card.hidden = !shouldShowClaudeCliCta();
        renderClaudeCliStatus();
    }

    function renderClaudeCliStatus() {
        const card = document.getElementById('wizard-claude-card');
        const statusEl = document.getElementById('wizard-claude-status');
        const installButton = document.getElementById('wizard-claude-install');
        const skipButton = document.getElementById('wizard-claude-skip');
        if (card) card.hidden = !shouldShowClaudeCliCta();
        if (statusEl) {
            statusEl.textContent = state.claudeCliStatusText || 'Проверка среды Claude...';
            statusEl.dataset.tone = state.claudeCliTone || 'muted';
        }
        if (installButton) {
            installButton.disabled = state.claudeCliBusy;
            installButton.textContent = state.claudeCliBusy
                ? 'Восстановление...'
                : (state.claudeCliInstalled ? 'Среда готова' : 'Восстановить среду');
        }
        if (skipButton) {
            skipButton.hidden = state.claudeCliBusy || state.claudeCliInstalled;
        }
    }

    function renderLocalStatus() {
        const statusEl = document.getElementById('wizard-local-status');
        const stopButton = document.getElementById('wizard-local-stop');
        const testButton = document.getElementById('wizard-local-test');
        const resultEl = document.getElementById('wizard-local-test-result');
        if (statusEl) {
            statusEl.textContent = state.localStatusText || 'Статус: Офлайн';
            statusEl.dataset.tone = state.localStatusTone || 'muted';
        }
        if (stopButton) stopButton.disabled = !state.localRuntimeReady;
        if (testButton) testButton.disabled = !state.localRuntimeReady;
        if (resultEl) {
            resultEl.hidden = !state.localTestResult;
            resultEl.dataset.tone = state.localTestTone || 'muted';
            resultEl.textContent = state.localTestResult || '';
        }
    }

    function setLocalTestResult(text, tone = 'muted') {
        state.localTestResult = text || '';
        state.localTestTone = tone;
        renderLocalStatus();
    }

    async function updateLocalStatus() {
        if (!LOCAL_RUNTIME_CONTROLS) return;
        try {
            const data = await apiRequest('/api/local-model/status', { cache: 'no-store' });
            const isReady = data.status === 'ready';
            let text = 'Статус: ' + ((data.status || 'offline').charAt(0).toUpperCase() + (data.status || 'offline').slice(1));
            if (data.status === 'ready' && data.context_length) text += ` (ctx: ${data.context_length})`;
            if (data.status === 'downloading' && data.download_progress) text += ` ${Math.round(data.download_progress * 100)}%`;
            if (data.error) text += ` - ${data.error}`;
            state.localRuntimeReady = isReady;
            state.localStatusText = text;
            state.localStatusTone = isReady ? 'ok' : (data.status === 'error' ? 'error' : 'muted');
            renderLocalStatus();
        } catch (error) {
            state.localRuntimeReady = false;
            state.localStatusText = `Статус: Ошибка - ${error.message}`;
            state.localStatusTone = 'error';
            renderLocalStatus();
        }
    }

    function readLocalModelBody() {
        return {
            source: trim(state.localSource),
            filename: trim(state.localFilename),
            port: 8766,
            n_gpu_layers: parseInt(state.localGpuLayers, 10),
            n_ctx: parseInt(state.localContextLength, 10) || 16384,
            chat_format: trim(state.localChatFormat),
        };
    }

    function startLocalStatusPolling() {
        if (!LOCAL_RUNTIME_CONTROLS || localStatusPollStarted) return;
        localStatusPollStarted = true;
        updateLocalStatus();
        setInterval(updateLocalStatus, 3000);
    }

    function renderLocalControls() {
        if (!LOCAL_RUNTIME_CONTROLS) return '';
        return `
            <div class="wizard-runtime-strip">
                <button type="button" class="btn btn-ghost" id="wizard-local-start">Запустить локальную среду</button>
                <button type="button" class="btn btn-ghost" id="wizard-local-stop" disabled>Остановить</button>
                <button type="button" class="btn btn-ghost" id="wizard-local-test" disabled>Тест вызова инструментов</button>
                <span id="wizard-local-status" class="wizard-runtime-status">Статус: Офлайн</span>
            </div>
            <div id="wizard-local-test-result" class="wizard-test-result"></div>
        `;
    }

    function renderClaudeCliControls() {
        return `
            <div class="panel-card" id="wizard-claude-card"${shouldShowClaudeCliCta() ? '' : ' hidden'}>
                <h3>Среда Claude</h3>
                <p>Среда Claude обеспечивает делегированное редактирование кода и рекомендательную проверку. Управляется приложением автоматически.</p>
                <div class="wizard-runtime-strip">
                    <button type="button" class="btn btn-ghost" id="wizard-claude-install" ${state.claudeCliBusy || state.claudeCliInstalled ? 'disabled' : ''}>
                        ${escapeHtml(state.claudeCliBusy ? 'Восстановление...' : (state.claudeCliInstalled ? 'Среда готова' : 'Восстановить среду'))}
                    </button>
                    <button type="button" class="btn btn-secondary" id="wizard-claude-skip" ${state.claudeCliBusy || state.claudeCliInstalled ? 'hidden' : ''}>Пропустить</button>
                    <span id="wizard-claude-status" class="wizard-runtime-status" data-tone="${escapeHtml(state.claudeCliTone || 'muted')}">${escapeHtml(state.claudeCliStatusText || 'Проверка среды Claude...')}</span>
                </div>
            </div>
        `;
    }

    function summaryRows() {
        const rows = [
            ['Обнаруженная конфигурация', profileLabel(activeProviderProfile())],
            ['Режим проверки', reviewLabel(state.reviewEnforcement)],
            ['Режим среды', runtimeModeLabel(state.runtimeMode)],
            ['Общий бюджет', formatUsd(state.totalBudget)],
            ['Мягкий порог на задачу', formatUsd(state.perTaskCostUsd)],
            ['Основная', trim(state.mainModel)],
            ['Код', trim(state.codeModel)],
            ['Лёгкая', trim(state.lightModel)],
            ['Запасная', trim(state.fallbackModel)],
        ];
        if (trim(state.openrouterKey)) rows.splice(1, 0, ['OpenRouter', 'настроен']);
        if (trim(state.openaiKey)) rows.splice(1, 0, ['OpenAI', 'настроен']);
        if (trim(state.cloudruKey)) rows.splice(1, 0, ['Cloud.ru', 'настроен']);
        if (trim(state.anthropicKey)) rows.splice(1, 0, ['Anthropic', 'настроен']);
        if (hasLocalModel()) {
            rows.splice(
                1,
                0,
                ['Локальный источник', trim(state.localSource) + (trim(state.localFilename) ? ` / ${trim(state.localFilename)}` : '')],
                ['Локальная маршрутизация', localRoutingLabel(state.localRoutingMode)],
            );
        }
        if (trim(state.skillsRepoPath)) {
            rows.push(['Репозиторий навыков', trim(state.skillsRepoPath)]);
        }
        return rows;
    }

        function providerKeyField({ id, label, placeholder, value, note }) {
            return `
                <div class="field">
                <div class="field-label-row">
                    <label for="${escapeHtml(id)}">${escapeHtml(label)}</label>
                    <button class="field-clear" data-clear="${escapeHtml(id)}" type="button">Clear</button>
                </div>
                <input id="${escapeHtml(id)}" type="password" placeholder="${escapeHtml(placeholder)}" value="${escapeHtml(value)}">
                <div class="field-note">${escapeHtml(note)}</div>
            </div>
            `;
        }

        function localInputField([id, stateKey, label, placeholder, note, className, type = 'text', min = '', step = '']) {
            const clear = ['local-source', 'local-filename', 'local-chat-format'].includes(id)
                ? `<button class="field-clear" data-clear="${id}" type="button">Clear</button>`
                : '';
            return `
                <div class="${className}">
                    <div class="field-label-row"><label for="${id}">${label}</label>${clear}</div>
                    <input id="${id}" type="${type}" ${min ? `min="${min}"` : ''} ${step ? `step="${step}"` : ''} placeholder="${placeholder}" value="${escapeHtml(state[stateKey])}">
                    ${note ? `<div class="field-note">${note}</div>` : ''}
                </div>
            `;
        }

        function renderProvidersStep() {
        const selectedProfile = activeProviderProfile();
        const localPreset = trim(state.localPreset);
        const localSourceOpen = state.localSourceOpen || hasLocalModel();
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.providers.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.providers.copy)}</p>
                </div>
            </div>
            <div class="panel-card">
                <h3>Сначала ключи, потом маршрутизация</h3>
                <p>${escapeHtml(PROVIDER_PROFILES[selectedProfile]?.providerCopy || '')}</p>
            </div>
            <div class="field-grid">
                ${PROVIDER_FIELDS.map((field) => providerKeyField({
                    ...field,
                    value: state[field.stateKey],
                })).join('')}
            </div>
            ${renderClaudeCliControls()}
            <details class="wizard-collapse" ${localSourceOpen ? 'open' : ''}>
                <summary>
                    <span>Настройки локальной модели</span>
                    <span class="selection-badge">${hasLocalModel() ? 'Настроено' : 'Необязательно'}</span>
                </summary>
                <div class="wizard-collapse-body">
                    <div class="field-grid">
                        <div class="field">
                            <div class="field-label-row">
                                <label for="local-preset">Пресет</label>
                                <button class="field-clear" data-clear="local-preset" type="button">Очистить</button>
                            </div>
                            <select id="local-preset">
                                <option value="" ${localPreset === '' ? 'selected' : ''}>Нет</option>
                                ${Object.entries(LOCAL_PRESETS).map(([id, preset]) => `<option value="${escapeHtml(id)}" ${localPreset === id ? 'selected' : ''}>${escapeHtml(preset.label)}</option>`).join('')}
                                <option value="custom" ${localPreset === 'custom' ? 'selected' : ''}>Пользовательский источник</option>
                            </select>
                            <div class="field-note">Большинству пользователей не нужно. Открывайте только если хотите локальную GGUF маршрутизацию.</div>
                        </div>
                        <div class="field">
                            <div class="field-label-row"><label>Локальная маршрутизация</label></div>
                            <div class="selection-row">
                                ${LOCAL_ROUTING_MODES.map((mode) => `<button class="selection-pill ${state.localRoutingMode === mode.value ? 'active' : ''}" data-local-mode="${escapeHtml(mode.value)}" type="button">${escapeHtml(mode.buttonLabel || mode.label)}</button>`).join('')}
                            </div>
                            <div class="field-note">Игнорируется, если источник локальной модели не настроен ниже.</div>
                        </div>
                        ${LOCAL_FIELDS.map(localInputField).join('')}
                    </div>
                    ${renderLocalControls()}
                </div>
            </details>
        `;
    }

    function modelSuggestionField({ id, label, value, note }) {
        return `
            <div class="field wizard-model-field" data-wizard-model-field>
                <label for="${escapeHtml(id)}">${escapeHtml(label)}</label>
                <input id="${escapeHtml(id)}" value="${escapeHtml(value)}" autocomplete="off" spellcheck="false" data-wizard-model-input>
                <div class="wizard-model-suggestions" hidden></div>
                <div class="field-note">${escapeHtml(note)}</div>
            </div>
        `;
    }

        function renderModelsStep() {
            const profile = activeProviderProfile();
            return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.models.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.models.copy)}</p>
                </div>
            </div>
            <div class="panel-card">
                <h3>Текущий профиль</h3>
                <p>${escapeHtml(
                    activeProviderProfile() === 'openai'
                        ? 'Обнаружена конфигурация только OpenAI. Значения по умолчанию явные и официальные.'
                        : activeProviderProfile() === 'cloudru'
                            ? 'Обнаружена конфигурация только Cloud.ru. Значения по умолчанию используют явные ID моделей cloudru::.'
                        : activeProviderProfile() === 'anthropic'
                            ? 'Обнаружена конфигурация только Anthropic. Значения по умолчанию явные и официальные.'
                        : activeProviderProfile() === 'direct-multi'
                                ? 'Настроено несколько прямых провайдеров. Начните здесь, затем при необходимости распределите слоты моделей между ними.'
                                : activeProviderProfile() === 'local'
                                    ? 'Обнаружена только-локальная конфигурация. Проверьте значения моделей и локальную маршрутизацию перед запуском.'
                                    : 'Маршрутизация в стиле OpenRouter остаётся активной. ID провайдеров без префикса, такие как openai/gpt-5.5 или anthropic/claude-sonnet-4.6, продолжают маршрутизироваться через OpenRouter.'
                )}</p>
            </div>
            <div class="grid two">
                ${modelSuggestionField({ id: 'main-model', label: 'Основная модель', value: state.mainModel, note: 'Основная для рассуждений и длинных задач.' })}
                ${modelSuggestionField({ id: 'code-model', label: 'Модель кода', value: state.codeModel, note: 'Для задач с большим количеством инструментов.' })}
                ${modelSuggestionField({ id: 'light-model', label: 'Лёгкая модель', value: state.lightModel, note: 'Быстрые резюме и лёгкие задачи.' })}
                ${modelSuggestionField({ id: 'fallback-model', label: 'Запасная модель', value: state.fallbackModel, note: 'Используется, если основная модель недоступна.' })}
            </div>
            <div class="wizard-inline-note">Прямые провайдеры используют <code>openai::gpt-5.5</code>, <code>cloudru::zai-org/GLM-4.7</code> и <code>anthropic::claude-sonnet-4-6</code>. Простые значения <code>openai/...</code> или <code>anthropic/...</code> остаются в стиле роутера по замыслу.</div>
        `;
    }

    function renderReviewModeStep() {
        const runtimeMode = trim(state.runtimeMode) || 'advanced';
        const runtimeModeCopy = HOST_MODE === 'desktop'
            ? 'Отдельная ось от режима проверки. Этот первоначальный выбор становится базовым при загрузке до запуска Ouroboros; последующее повышение требует нативного подтверждения лаунчера.'
            : 'Отдельная ось от режима проверки. Веб/Docker-настройка сохраняет это через owner-эндпоинт; выбранный режим активируется после перезапуска.';
        const disabledAttr = '';
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.review_mode.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.review_mode.copy)}</p>
                </div>
            </div>
            <div class="wizard-choice-grid">
                <button type="button" class="wizard-choice advisory ${state.reviewEnforcement === 'advisory' ? 'active' : ''}" data-review-mode="advisory">
                    <span class="tone">Гибкий</span>
                    <h3>Рекомендательный</h3>
                    <p>Быстрее и дешевле. Проверка всё равно выполняется, но вы сами решаете, что делать с замечаниями. Лучший выбор, когда важна скорость итераций.</p>
                </button>
                <button type="button" class="wizard-choice blocking ${state.reviewEnforcement === 'blocking' ? 'active' : ''}" data-review-mode="blocking">
                    <span class="tone">Строгий</span>
                    <h3>Блокирующий</h3>
                    <p>Медленнее и дороже, но намного безопаснее. Критические замечания останавливают коммиты, что значительно снижает риск постепенной деградации кода.</p>
                </button>
            </div>
            <div class="panel-card runtime-mode-card">
                <h3>Режим среды выполнения</h3>
                <p class="field-note">${escapeHtml(runtimeModeCopy)}</p>
                <div class="wizard-choice-grid three">
                    <button type="button" class="wizard-choice light ${runtimeMode === 'light' ? 'active' : ''}" data-runtime-mode="light"${disabledAttr}>
                        <span class="tone">Безопаснее</span>
                        <h3>Light</h3>
                        <p>Самомодификация основного репозитория отключена. Лучший вариант для знакомства с Ouroboros или использования как чистого ассистента.</p>
                    </button>
                    <button type="button" class="wizard-choice advanced ${runtimeMode === 'advanced' ? 'active' : ''}" data-runtime-mode="advanced"${disabledAttr}>
                        <span class="tone">По умолчанию</span>
                        <h3>Advanced</h3>
                        <p>Самомодификация эволюционного слоя разрешена (текущее поведение). Защищённые файлы ядра/контрактов/релизов охраняются в режиме Advanced.</p>
                    </button>
                    <button type="button" class="wizard-choice pro ${runtimeMode === 'pro' ? 'active' : ''}" data-runtime-mode="pro"${disabledAttr}>
                        <span class="tone">Расширенный</span>
                        <h3>Pro</h3>
                        <p>Прямой режим защищённых поверхностей. Редактирование защищённых файлов ядра/контрактов/релизов разрешено, но коммиты по-прежнему проходят через триаду и проверку области.</p>
                    </button>
                </div>
                <div class="field">
                    <div class="field-label-row">
                        <label for="skills-repo-path">Внешний репозиторий навыков (необязательно)</label>
                        <button class="field-clear" data-clear="skills-repo-path" type="button">Очистить</button>
                    </div>
                    <input id="skills-repo-path" type="text" placeholder="~/Ouroboros/skills или /абсолютный/путь/к/навыкам" value="${escapeHtml(state.skillsRepoPath || '')}">
                    <div class="field-note">Необязательно. Дополнительный корень поиска поверх дерева <code>data/skills/{native,clawhub,external}/</code>. Оставьте пустым, если не поддерживаете собственную копию навыков — Ouroboros никогда не клонирует/не обновляет эту директорию.</div>
                </div>
            </div>
        `;
    }

        function renderBudgetStep() {
            return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.budget.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.budget.copy)}</p>
                </div>
            </div>
            <div class="grid two">
                <div class="panel-card">
                    <h3>Общий бюджет</h3>
                    <div class="field">
                        <label for="total-budget">Общий бюджет (USD)</label>
                        <input id="total-budget" type="number" min="1" step="1" value="${escapeHtml(state.totalBudget)}">
                        <div class="field-note">Глобальный бюджет расходов для всей среды. Оставляйте редактируемым даже после настройки.</div>
                    </div>
                </div>
                <div class="panel-card">
                    <h3>Мягкий порог на задачу</h3>
                    <div class="field">
                        <label for="per-task-budget">Лимит затрат на задачу (USD)</label>
                        <input id="per-task-budget" type="number" min="1" step="1" value="${escapeHtml(state.perTaskCostUsd)}">
                        <div class="field-note">Не останавливает задачу жёстко. Вставляет напоминание о бюджете, когда задача начинает становиться дорогой.</div>
                    </div>
                </div>
            </div>
        `;
    }

    function renderSummaryStep() {
        const summary = summaryRows().map(([label, value]) => `
            <div class="summary-kv">
                <strong>${escapeHtml(label)}</strong>
                <span>${escapeHtml(value)}</span>
            </div>
        `).join('');
        return `
            <div class="step-header">
                <div>
                    <h2 class="step-title">${escapeHtml(STEP_META.summary.title)}</h2>
                    <p class="step-copy">${escapeHtml(STEP_META.summary.copy)}</p>
                </div>
            </div>
            <div class="summary-card">${summary}</div>
        `;
    }

    function renderStepContent() {
        if (state.currentStep === 'providers') return renderProvidersStep();
        if (state.currentStep === 'models') return renderModelsStep();
        if (state.currentStep === 'review_mode') return renderReviewModeStep();
        if (state.currentStep === 'budget') return renderBudgetStep();
        return renderSummaryStep();
    }

    function stepCards() {
        return STEP_ORDER.map((stepId, index) => {
            const active = stepId === state.currentStep;
            const done = STEP_ORDER.indexOf(state.currentStep) > index;
            const meta = STEP_META[stepId];
            return `
                <div class="wizard-step ${active ? 'active' : ''} ${done ? 'done' : ''}">
                    <div class="wizard-step-index">Шаг ${index + 1}</div>
                    <p class="wizard-step-title">${escapeHtml(meta.title)}</p>
                    <p class="wizard-step-copy">${escapeHtml(meta.railCopy || '')}</p>
                </div>
            `;
        }).join('');
    }

    function render() {
        const meta = STEP_META[state.currentStep];
        const index = STEP_ORDER.indexOf(state.currentStep);
        const nextLabel = state.currentStep === 'summary'
            ? (state.saving ? 'Сохранение...' : 'Запустить Ouroboros')
            : 'Продолжить';
        root.innerHTML = `
            <div class="wizard-shell">
                <div class="wizard-header">
                    <div>
                        <h1 class="wizard-title">Ouroboros</h1>
                        <p class="wizard-subtitle">Единая настройка для десктопа и веба с одинаковым потоком выбора модели, проверки и бюджета.</p>
                    </div>
                    <div class="wizard-badge">Шаг ${index + 1} из ${STEP_ORDER.length}</div>
                </div>
                <div class="wizard-steps">${stepCards()}</div>
                <div class="wizard-content">
                    ${renderStepContent()}
                    <div class="wizard-footer">
                        <div class="footer-copy">${escapeHtml(meta.footer)}</div>
                        <div class="footer-actions">
                            <button class="btn btn-secondary" id="back-btn" type="button" ${index === 0 || state.saving ? 'disabled' : ''}>Назад</button>
                            <button class="btn btn-primary" id="next-btn" type="button" ${nextButtonShouldBeDisabled() ? 'disabled' : ''}>${escapeHtml(nextLabel)}</button>
                        </div>
                    </div>
                    <div class="wizard-error">${escapeHtml(state.error)}</div>
                </div>
            </div>
        `;
        bindEvents();
        renderLocalStatus();
        renderClaudeCliStatus();
    }

        function bindClearButtons() {
            const clearActions = Object.fromEntries(PROVIDER_FIELDS.map((field) => [
                field.id,
                () => { state[field.stateKey] = ''; },
            ]));
            Object.assign(clearActions, {
                'local-preset': () => {
                    state.localPreset = '';
                    state.localSource = '';
                state.localFilename = '';
                state.localRoutingMode = 'cloud';
                state.localSourceOpen = false;
            },
            'local-source': () => {
                state.localSource = '';
                state.localPreset = detectLocalPresetSelection();
            },
            'local-filename': () => {
                state.localFilename = '';
                state.localPreset = detectLocalPresetSelection();
                },
                'local-chat-format': () => { state.localChatFormat = ''; },
                'skills-repo-path': () => { state.skillsRepoPath = ''; },
            });
        root.querySelectorAll('[data-clear]').forEach((button) => {
            button.addEventListener('click', () => {
                const target = button.getAttribute('data-clear');
                if (clearActions[target]) clearActions[target]();
                state.error = '';
                render();
            });
        });
    }

    function bindProvidersStep() {
        const details = root.querySelector('.wizard-collapse');
        if (details) {
            details.addEventListener('toggle', () => {
                state.localSourceOpen = details.open;
            });
        }
            const localPreset = document.getElementById('local-preset');
            const localSource = document.getElementById('local-source');
        const localFilename = document.getElementById('local-filename');
        const localContext = document.getElementById('local-context');
        const localGpuLayers = document.getElementById('local-gpu-layers');
        const localChatFormat = document.getElementById('local-chat-format');

        function bindStateInput(input, key, after = null) {
            if (!input) return;
            input.addEventListener('input', () => {
                state[key] = input.value;
                if (after) after(input);
                markStepEdited();
            });
        }

            PROVIDER_FIELDS.forEach((field) => {
                const input = document.getElementById(field.id);
                if (field.settingKey !== 'ANTHROPIC_API_KEY') {
                    bindStateInput(input, field.stateKey);
                    return;
                }
                if (!input) return;
                input.addEventListener('input', () => {
                    const wasConfigured = hasAnthropicKeyConfigured();
                    state[field.stateKey] = input.value;
                    if (!wasConfigured && hasAnthropicKeyConfigured()) {
                        state.claudeCliDismissed = false;
                        startClaudeCliStatusPolling();
                        updateClaudeCliStatus();
                    }
                    syncClaudeCliVisibility();
                    markStepEdited();
                });
            });
        if (localPreset) localPreset.addEventListener('change', () => { applyPresetSelection(localPreset.value); state.error = ''; render(); });
        bindStateInput(localSource, 'localSource', () => {
            state.localPreset = detectLocalPresetSelection();
            if (localPreset) localPreset.value = state.localPreset || '';
            state.localSourceOpen = true;
            if (trim(state.localSource) && activeProviderProfile() === 'local' && trim(state.localRoutingMode) === 'cloud') {
                state.localRoutingMode = 'all';
            }
        });
        bindStateInput(localFilename, 'localFilename', () => {
            state.localPreset = detectLocalPresetSelection();
            if (localPreset) localPreset.value = state.localPreset || '';
        });
        bindStateInput(localContext, 'localContextLength');
        bindStateInput(localGpuLayers, 'localGpuLayers');
        bindStateInput(localChatFormat, 'localChatFormat');
        root.querySelectorAll('[data-local-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                state.localRoutingMode = button.getAttribute('data-local-mode');
                state.error = '';
                render();
            });
        });
        if (LOCAL_RUNTIME_CONTROLS) {
            startLocalStatusPolling();
            document.getElementById('wizard-local-start')?.addEventListener('click', async () => {
                const body = readLocalModelBody();
                if (!body.source) {
                    state.error = 'Укажите источник локальной модели перед запуском локальной среды.';
                    render();
                    return;
                }
                setLocalTestResult('', 'muted');
                try {
                    const resp = await fetch('/api/local-model/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (resp.status === 412 && data.error === 'runtime_missing') {
                        setLocalTestResult(
                            'Локальная среда (llama-cpp-python) не установлена.\n' +
                            'Перейдите в Настройки → Расширенные → Локальная среда выполнения модели\n' +
                            'и нажмите «Установить локальную среду».\n\n' +
                            'Вручную: ' + (data.hint || 'pip install llama-cpp-python[server]'),
                            'error'
                        );
                    } else if (data.error) {
                        setLocalTestResult(`Ошибка запуска: ${data.error}`, 'error');
                    } else {
                        updateLocalStatus();
                    }
                } catch (error) {
                    setLocalTestResult(`Ошибка запуска: ${error.message}`, 'error');
                }
            });
            document.getElementById('wizard-local-stop')?.addEventListener('click', async () => {
                try {
                    await apiRequest('/api/local-model/stop', { method: 'POST' });
                    updateLocalStatus();
                } catch (error) {
                    setLocalTestResult(`Ошибка остановки: ${error.message}`, 'error');
                }
            });
            document.getElementById('wizard-local-test')?.addEventListener('click', async () => {
                setLocalTestResult('Выполняется тестирование...', 'muted');
                try {
                    const result = await apiRequest('/api/local-model/test', { method: 'POST' });
                    const lines = [];
                    lines.push(`${result.chat_ok ? '✓' : '✗'} Базовый чат${result.tokens_per_sec ? ` (${result.tokens_per_sec} tok/s)` : ''}`);
                    lines.push(`${result.tool_call_ok ? '✓' : '✗'} Вызов инструментов`);
                    if (result.details && !result.success) lines.push(result.details);
                    setLocalTestResult(lines.join('\n'), result.success ? 'ok' : 'warn');
                } catch (error) {
                    setLocalTestResult(`Тест не пройден: ${error.message}`, 'error');
                }
            });
        }
        document.getElementById('wizard-claude-install')?.addEventListener('click', async () => {
            state.claudeCliBusy = true;
            state.claudeCliTone = 'muted';
            state.claudeCliStatusText = 'Восстановление среды Claude...';
            renderClaudeCliStatus();
            try {
                applyClaudeCliStatus(await claudeCliStartInstall());
                if (state.claudeCliBusy) updateClaudeCliStatus();
            } catch (error) {
                state.claudeCliBusy = false;
                state.claudeCliStatus = 'error';
                state.claudeCliError = String(error?.message || error || '');
                state.claudeCliTone = 'error';
                state.claudeCliStatusText = `Ошибка восстановления среды Claude: ${state.claudeCliError}`;
                renderClaudeCliStatus();
            }
        });
        document.getElementById('wizard-claude-skip')?.addEventListener('click', () => {
            state.claudeCliDismissed = true;
            syncClaudeCliVisibility();
        });
        if (shouldShowClaudeCliCta()) {
            startClaudeCliStatusPolling();
            updateClaudeCliStatus();
        } else {
            renderClaudeCliStatus();
        }
        syncCurrentStepActionState();
    }

        function bindModelsStep() {
            const modelInputMap = Object.fromEntries(MODEL_SLOTS.map((slot) => [slot.inputId, slot.stateKey]));
            function suggestionMatches(query) {
                const needle = trim(query).toLowerCase();
                return MODEL_SUGGESTIONS
                    .filter((model) => !needle || String(model).toLowerCase().includes(needle))
                    .slice(0, 8);
            }
        function closeSuggestions(exceptInput = null) {
            root.querySelectorAll('.wizard-model-suggestions').forEach((panel) => {
                if (exceptInput && panel.parentElement?.querySelector('input') === exceptInput) return;
                panel.hidden = true;
                panel.innerHTML = '';
            });
        }
        function renderSuggestions(input) {
            const panel = input.closest('[data-wizard-model-field]')?.querySelector('.wizard-model-suggestions');
            if (!panel) return;
            const matches = suggestionMatches(input.value);
            if (!matches.length) {
                panel.hidden = true;
                panel.innerHTML = '';
                return;
            }
            panel.innerHTML = matches.map((model) => (
                `<button type="button" class="wizard-model-suggestion" data-value="${escapeHtml(model)}">${escapeHtml(model)}</button>`
            )).join('');
            panel.hidden = false;
        }
            Object.entries(modelInputMap).forEach(([id, key]) => {
            const input = document.getElementById(id);
            if (!input) return;
            input.addEventListener('focus', () => {
                closeSuggestions(input);
                renderSuggestions(input);
            });
            input.addEventListener('input', () => {
                state[key] = input.value;
                state.modelsDirty = true;
                state.error = '';
                closeSuggestions(input);
                renderSuggestions(input);
                syncCurrentStepActionState();
            });
        });
        root.querySelectorAll('.wizard-model-suggestions').forEach((panel) => {
            panel.addEventListener('mousedown', (event) => {
                const button = event.target.closest('.wizard-model-suggestion');
                if (!button) return;
                event.preventDefault();
                const input = panel.parentElement?.querySelector('input');
                if (!input) return;
                input.value = button.dataset.value || '';
                input.dispatchEvent(new Event('input', { bubbles: true }));
                closeSuggestions();
            });
        });
        if (root.dataset.modelSuggestionOutsideListener !== '1') {
            root.dataset.modelSuggestionOutsideListener = '1';
            document.addEventListener('mousedown', (event) => {
                if (!root.contains(event.target) || !event.target.closest('[data-wizard-model-field]')) {
                    root.querySelectorAll('.wizard-model-suggestions').forEach((panel) => {
                        panel.hidden = true;
                        panel.innerHTML = '';
                    });
                }
            });
        }
        syncCurrentStepActionState();
    }

    function bindReviewModeStep() {
        root.querySelectorAll('[data-review-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                state.reviewEnforcement = button.getAttribute('data-review-mode');
                state.error = '';
                render();
            });
        });
        root.querySelectorAll('[data-runtime-mode]').forEach((button) => {
            button.addEventListener('click', () => {
                state.runtimeMode = button.getAttribute('data-runtime-mode');
                state.error = '';
                render();
            });
        });
        const skillsInput = document.getElementById('skills-repo-path');
        if (skillsInput) skillsInput.addEventListener('input', () => { state.skillsRepoPath = skillsInput.value; markStepEdited(); });
        syncCurrentStepActionState();
    }

        function bindBudgetStep() {
            BUDGET_FIELDS.forEach((field) => {
                const input = document.getElementById(field.inputId);
                if (input) input.addEventListener('input', () => { state[field.stateKey] = input.value; markStepEdited(); });
            });
            syncCurrentStepActionState();
        }

    async function saveWizardPayload(payload) {
        if (HOST_MODE === 'web') {
            const runtimeMode = trim(state.runtimeMode) || 'advanced';
            await apiRequest('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const runtimeResult = await apiRequest('/api/owner/runtime-mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode: runtimeMode }),
            });
            window.parent?.postMessage({
                type: 'ouroboros:onboarding-complete',
                restart_required: Boolean(runtimeResult?.restart_required),
                runtime_mode: runtimeResult?.runtime_mode || runtimeMode,
            }, '*');
            if (!window.parent || window.parent === window) {
                window.location.replace('/');
            }
            return 'ok';
        }
        if (!window.pywebview?.api?.save_wizard) {
            throw new Error('Desktop onboarding bridge is unavailable.');
        }
        const result = await window.pywebview.api.save_wizard(payload);
        if (result !== 'ok') throw new Error(result || 'Не удалось сохранить настройки онбординга.');
        return result;
    }

    async function saveWizard() {
        const providersError = validateProvidersStep();
        const modelsError = validateModelsStep();
        const reviewError = validateReviewStep();
        const budgetError = validateBudgetStep();
        state.error = providersError || modelsError || reviewError || budgetError;
        if (state.error) {
            render();
            return;
        }
            state.saving = true;
            state.error = '';
            render();
            const payload = {
                ...Object.fromEntries(PROVIDER_FIELDS.map((field) => [field.settingKey, trim(state[field.stateKey])])),
                ...Object.fromEntries(BUDGET_FIELDS.map((field) => [field.settingKey, Number(state[field.stateKey] || 0)])),
                OUROBOROS_REVIEW_ENFORCEMENT: trim(state.reviewEnforcement) || 'advisory',
                OUROBOROS_SKILLS_REPO_PATH: trim(state.skillsRepoPath),
                LOCAL_MODEL_SOURCE: trim(state.localSource),
            LOCAL_MODEL_FILENAME: trim(state.localFilename),
            LOCAL_MODEL_CONTEXT_LENGTH: Number(state.localContextLength || 0),
                LOCAL_MODEL_N_GPU_LAYERS: Number(state.localGpuLayers || 0),
                LOCAL_MODEL_CHAT_FORMAT: trim(state.localChatFormat),
                LOCAL_ROUTING_MODE: trim(state.localSource) ? (trim(state.localRoutingMode) || 'cloud') : 'cloud',
                ...Object.fromEntries(MODEL_SLOTS.map((slot) => [slot.settingKey, trim(state[slot.stateKey])])),
            };
        payload.OUROBOROS_RUNTIME_MODE = trim(state.runtimeMode) || 'advanced';
        try {
            await saveWizardPayload(payload);
        } catch (error) {
            state.saving = false;
            state.error = String(error?.message || error || 'Не удалось сохранить настройки онбординга.');
            render();
        }
    }

    function bindEvents() {
        bindClearButtons();
        document.getElementById('back-btn')?.addEventListener('click', previousStep);
        document.getElementById('next-btn')?.addEventListener('click', () => {
            if (state.currentStep === 'summary') saveWizard();
            else nextStep();
        });
        if (state.currentStep === 'providers') bindProvidersStep();
        if (state.currentStep === 'models') bindModelsStep();
        if (state.currentStep === 'review_mode') bindReviewModeStep();
        if (state.currentStep === 'budget') bindBudgetStep();
        syncCurrentStepActionState();
    }

    applyModelDefaults(false);
    render();
})();
