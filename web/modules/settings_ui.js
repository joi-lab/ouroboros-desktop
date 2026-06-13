import { renderPageHeader, renderTabStrip } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';

const SETTINGS_TABS = [
    { value: 'providers', label: 'Провайдеры' },
    { value: 'secrets', label: 'Секреты' },
    { value: 'models', label: 'Модели' },
    { value: 'behavior', label: 'Поведение' },
    { value: 'advanced', label: 'Расширенные' },
    { value: 'about', label: 'О программе' },
];
// Guard markers: renderTabStrip emits behavior/advanced tabs at runtime.

const MODEL_CARDS = [
    ['Основная', 'Основная модель для рассуждений.', 's-model', 's-local-main', 'anthropic/claude-opus-4.6'],
    ['Код', 'Модель для задач с большим количеством инструментов.', 's-model-code', 's-local-code', 'anthropic/claude-opus-4.6'],
    ['Лёгкая', 'Быстрые резюме и лёгкие задачи.', 's-model-light', 's-local-light', 'anthropic/claude-sonnet-4.6'],
    ['Запасная', 'Используется, если основная модель недоступна.', 's-model-fallback', 's-local-fallback', 'anthropic/claude-sonnet-4.6'],
];

const EFFORT_FIELDS = [
    ['s-effort-task', 'Задача / Чат', 'medium'],
    ['s-effort-evolution', 'Эволюция', 'high'],
    ['s-effort-review', 'Проверка', 'medium'],
    ['s-effort-scope-review', 'Проверка области', 'high'],
    ['s-effort-consciousness', 'Сознание', 'low'],
];

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
                <button type="button" class="settings-ghost-btn secret-toggle" data-target="${id}">Показать</button>
                <button type="button" class="settings-ghost-btn secret-clear" data-target="${id}">Очистить</button>
            </div>
        </div>
    `;
}

function plainField({ id, label, placeholder }) {
    return `<div class="form-field"><label>${label}</label><input id="${id}" placeholder="${placeholder}"></div>`;
}

const PROVIDER_CARDS = [
    {
        id: 'openrouter', title: 'OpenRouter', icon: '/static/providers/openrouter.ico', hint: 'Роутер по умолчанию для нескольких моделей', open: true,
        fields: [{ id: 's-openrouter', settingKey: 'OPENROUTER_API_KEY', label: 'OpenRouter API Key', placeholder: 'sk-or-...' }],
    },
    {
        id: 'openai', title: 'OpenAI', icon: '/static/providers/openai.svg', hint: 'Официальный OpenAI API',
        fields: [{ id: 's-openai', settingKey: 'OPENAI_API_KEY', label: 'OpenAI API Key', placeholder: 'sk-...' }],
        note: 'Используйте значения моделей вида <code>openai::gpt-5.5</code> во вкладке «Модели» для прямой маршрутизации. Если OpenRouter отсутствует и значения по умолчанию не изменялись, Ouroboros автоматически переключается на официальные модели OpenAI.',
    },
    {
        id: 'compatible', title: 'OpenAI Compatible', icon: '/static/providers/openai-compatible.svg', hint: 'Пользовательский OpenAI-совместимый эндпоинт',
        fields: [
            { id: 's-openai-compatible-key', settingKey: 'OPENAI_COMPATIBLE_API_KEY', label: 'API Key', placeholder: 'Ключ совместимого провайдера' },
            { id: 's-openai-compatible-base-url', label: 'Base URL', placeholder: 'https://provider.example/v1' },
        ],
        note: 'Используйте эту карточку для пользовательских Base URL. Встроенный веб-поиск работает только с официальным OpenAI Responses API, поэтому оставьте <code>OPENAI_BASE_URL</code> пустым, если нужен <code>web_search</code>.',
    },
    {
        id: 'cloudru', title: 'Cloud.ru Foundation Models', icon: '/static/providers/cloudru.svg', hint: 'Cloud.ru OpenAI-совместимая среда',
        fields: [
            { id: 's-cloudru-key', settingKey: 'CLOUDRU_FOUNDATION_MODELS_API_KEY', label: 'API Key', placeholder: 'API-ключ Cloud.ru Foundation Models' },
            { id: 's-cloudru-base-url', label: 'Base URL', placeholder: 'https://foundation-models.api.cloud.ru/v1' },
        ],
    },
    {
        id: 'anthropic', title: 'Anthropic', icon: '/static/providers/anthropic.png', hint: 'Прямая среда и инструменты Claude',
        fields: [{ id: 's-anthropic', settingKey: 'ANTHROPIC_API_KEY', label: 'Anthropic API Key', placeholder: 'sk-ant-...' }],
        note: 'Используйте значения моделей вида <code>anthropic::claude-sonnet-4-6</code> во вкладке «Модели» для прямой маршрутизации через Anthropic. Инструменты Claude также используют этот ключ.',
        extra: `
            <div class="settings-toolbar" id="settings-claude-code-panel" hidden>
                <button type="button" class="settings-ghost-btn" id="btn-claude-code-install">Восстановить среду</button>
                <span id="settings-claude-code-status" class="settings-inline-status">Проверка среды Claude...</span>
            </div>
            <div class="settings-inline-note" id="settings-claude-code-copy" hidden>Среда Claude обеспечивает делегированное редактирование кода и рекомендательную проверку. Управляется приложением автоматически.</div>
        `,
    },
];

function providerSettingsCard(spec) {
    const fields = (spec.fields || [])
        .map((field) => field.settingKey ? secretField(field) : plainField(field))
        .join('');
    return providerCard({
        id: spec.id,
        title: spec.title,
        icon: spec.icon,
        hint: spec.hint,
        open: spec.open,
        body: `<div class="form-row">${fields}</div>${spec.note ? `<div class="settings-inline-note">${spec.note}</div>` : ''}${spec.extra || ''}`,
    });
}

function modelCard({ title, copy, inputId, toggleId, defaultValue }) {
    return `
        <div class="settings-model-card">
            <div class="settings-model-header">
                <div>
                    <h4>${title}</h4>
                    <p>${copy}</p>
                </div>
                <label class="local-toggle"><input type="checkbox" id="${toggleId}"> Локальная</label>
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
                <button type="button" class="settings-effort-btn" data-effort-value="none">Нет</button>
                <button type="button" class="settings-effort-btn" data-effort-value="low">Низкий</button>
                <button type="button" class="settings-effort-btn" data-effort-value="medium">Средний</button>
                <button type="button" class="settings-effort-btn" data-effort-value="high">Высокий</button>
            </div>
        </div>
    `;
}

export const SECRET_KEYS = [
    ['OPENROUTER_API_KEY', 'OpenRouter API Key', 'sk-or-...'],
    ['OPENAI_API_KEY', 'OpenAI API Key', 'sk-...'],
    ['OPENAI_COMPATIBLE_API_KEY', 'OpenAI-compatible API Key', 'Ключ совместимого провайдера'],
    ['CLOUDRU_FOUNDATION_MODELS_API_KEY', 'Cloud.ru Foundation Models API Key', 'Ключ Cloud.ru'],
    ['ANTHROPIC_API_KEY', 'Anthropic API Key', 'sk-ant-...'],
    ['GITHUB_TOKEN', 'GitHub Token', 'ghp_...'],
    ['OUROBOROS_NETWORK_PASSWORD', 'Сетевой пароль', 'Требуется для LAN/Docker доступа'],
];

function secretSettingsSection() {
    return `
        <section class="settings-card">
            <h3>Сохранённые секреты</h3>
            <div class="settings-section-copy">
                Центральное хранилище API-ключей, токенов, паролей и секретов, запрошенных навыками.
                Навыки получают ключи только после явного одобрения пользователем.
            </div>
            <div class="form-grid two">
                ${SECRET_KEYS.map(([key, label, placeholder]) => secretField({
                    id: `s-secret-${key.toLowerCase().replace(/_/g, '-')}`,
                    settingKey: key,
                    label,
                    placeholder,
                })).join('')}
            </div>
        </section>
        <section class="settings-card">
            <h3>Запрошено навыками</h3>
            <div class="settings-section-copy">
                Секреты, запрошенные установленными навыками, появляются здесь только когда навык их запрашивает.
            </div>
            <div id="skill-requested-secrets" class="settings-secret-list">
                <div class="muted">Нет секретов, запрошенных навыками.</div>
            </div>
        </section>
        <section class="settings-card">
            <div class="settings-card-head">
                <div>
                    <h3>Пользовательские ключи</h3>
                    <div class="settings-section-copy">
                        Необязательное хранилище ключей/значений для навыков. Используйте имена в верхнем регистре, например <code>SLACK_WEBHOOK_URL</code>.
                    </div>
                </div>
                <button type="button" class="btn btn-default btn-sm" id="btn-add-custom-secret">Добавить ключ</button>
            </div>
            <div id="custom-secrets-list" class="settings-secret-list settings-custom-secret-list"></div>
        </section>
    `;
}

export function renderSettingsPage() {
    return `
        ${renderPageHeader({
            title: 'Настройки',
            icon: PAGE_ICONS.settings,
            description: 'Настройте провайдеров, секреты, модели, поведение, систему контроля версий и параметры среды выполнения.',
            tabsHtml: `
                <div class="settings-tabs-bar">
                    <button type="button" class="settings-mobile-back" data-settings-back hidden>Настройки</button>
                    ${renderTabStrip({
                        items: SETTINGS_TABS,
                        active: 'providers',
                        dataAttr: 'data-settings-tab',
                        ariaLabel: 'Разделы настроек',
                        stripClass: 'settings-tabs',
                        tabClass: 'settings-tab',
                    })}
                </div>
            `,
        })}
        <div class="settings-shell">
            <div class="settings-scroll scroll-fade-y">
                <section class="settings-panel active" data-settings-panel="providers">
                    <div class="settings-section-copy">
                        Настройте удалённых провайдеров и дополнительный сетевой шлюз. Поля секретов теперь содержат явные
                        кнопки <code>Очистить</code> для намеренного удаления скрытых значений.
                    </div>
                    ${PROVIDER_CARDS.map(providerSettingsCard).join('')}
                    <div class="form-section compact">
                        <h3>Совместимость с устаревшими версиями</h3>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Устаревший OpenAI Base URL</label>
                                <input id="s-openai-base-url" placeholder="https://api.openai.com/v1 или совместимый эндпоинт">
                            </div>
                        </div>
                        <div class="settings-inline-note">Запасной вариант для совместимости со старыми установками. Для новых пользовательских провайдеров используйте карточку <code>OpenAI Compatible</code>.</div>
                    </div>
                    <div class="form-section compact">
                        <h3>Сетевой шлюз</h3>
                        <div class="form-row">${secretField({
                            id: 's-network-password',
                            settingKey: 'OUROBOROS_NETWORK_PASSWORD',
                            label: 'Сетевой пароль (необязательно)',
                            placeholder: 'Оставьте пустым для открытого доступа',
                        })}</div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Хост привязки сервера</label>
                                <input id="s-server-host" placeholder="127.0.0.1 или 0.0.0.0">
                                <div class="settings-inline-note">Используйте <code>127.0.0.1</code> только для этой машины. Используйте <code>0.0.0.0</code> для LAN/Docker-доступа с сетевым паролем. Привязка к конкретному LAN IP — только вручную через env.</div>
                            </div>
                        </div>
                        <div class="settings-inline-note">Добавляет защиту паролем только для нелокального доступа к приложению и API. Если Ouroboros доступен в LAN или Docker, установите пароль перед публикацией URL.</div>
                        <div id="settings-lan-hint" class="settings-lan-hint" hidden></div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="secrets">
                    ${secretSettingsSection()}
                </section>

                <section class="settings-panel" data-settings-panel="models">
                    <div class="form-section">
                        <h3>Маршрутизация моделей</h3>
                        <div class="settings-section-copy">
                            Это идентификаторы облачных моделей. Включите <code>Локальная</code>, чтобы направить модель
                            через GGUF-сервер, настроенный в разделе «Расширенные».
                        </div>
                        <div class="settings-toolbar">
                            <button type="button" class="settings-ghost-btn" id="btn-refresh-model-catalog">Обновить каталог моделей</button>
                            <span id="settings-model-catalog-status" class="settings-inline-status">Каталог моделей необязателен и устойчив к сбоям.</span>
                        </div>
                        <div class="settings-model-grid">
                            ${MODEL_CARDS.map(([title, copy, inputId, toggleId, defaultValue]) => modelCard({ title, copy, inputId, toggleId, defaultValue })).join('')}
                        </div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Модель Claude Code</label>
                                <input id="s-claude-code-model" value="opus[1m]" placeholder="sonnet, opus, opus[1m] или полное имя">
                                <div class="settings-inline-note">Модель Anthropic для инструментов <code>claude_code_edit</code> и <code>advisory_pre_review</code>. Требует ключ Anthropic в разделе «Провайдеры».</div>
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Модели для проверки</h3>
                        <div class="settings-section-copy">Слоты проверяющих для планирования, приёмки задач и проверки коммитов.</div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Слоты проверки</label>
                                <input id="s-review-models" placeholder="модель1,модель2,модель3">
                                <div class="settings-inline-note">Слоты проверяющих через запятую. Дублирование ID модели допустимо — это независимые слоты для сэмплирования одной и той же модели.</div>
                            </div>
                        </div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label>Слоты проверки области</label>
                                <input id="s-scope-review-models" placeholder="openai/gpt-5.5">
                                <div class="settings-inline-note">Слоты проверяющих область через запятую. Пустое значение откатывается к устаревшей настройке одной модели проверки области.</div>
                            </div>
                            <div class="form-field">
                                <label>Модель веб-поиска</label>
                                <input id="s-websearch-model" placeholder="gpt-5.2">
                                <div class="settings-inline-note">Модель OpenAI для <code>web_search</code>. Требует <code>OPENAI_API_KEY</code> и пустой устаревший Base URL.</div>
                            </div>
                        </div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="behavior">
                    <div class="form-section">
                        <h3>Уровень рассуждений</h3>
                        <div class="settings-section-copy">Управляет глубиной размышлений модели для каждого типа задач. Выше уровень — медленнее, но тщательнее.</div>
                        <div class="settings-effort-grid">
                            ${EFFORT_FIELDS.map(([id, label, defaultValue]) => effortField({ id, label, defaultValue })).join('')}
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Режим проверки</h3>
                        <div class="settings-section-copy"><code>Рекомендательный</code> — проверка видна, но не блокирует. <code>Блокирующий</code> — останавливает коммиты и активацию навыков при нерешённых критических замечаниях.</div>
                        <div class="settings-effort-card">
                            <label>Режим проверки</label>
                            <input id="s-review-enforcement" type="hidden" value="advisory">
                            <div class="settings-effort-group" data-effort-group data-enforcement-group data-effort-target="s-review-enforcement">
                                <button type="button" class="settings-effort-btn" data-effort-value="advisory">Рекомендательный</button>
                                <button type="button" class="settings-effort-btn" data-effort-value="blocking">Блокирующий</button>
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Проверка результата задачи</h3>
                        <div class="settings-section-copy">В режиме «Авто» решение остаётся за Ouroboros через видимый инструмент проверки; «Обязательно» вставляет вывод проверяющего перед выдачей подходящих результатов задач.</div>
                        <div class="settings-effort-card">
                            <label>Проверка результата задачи</label>
                            <input id="s-task-review-mode" type="hidden" value="auto">
                            <div class="settings-effort-group" data-effort-group data-task-review-group data-effort-target="s-task-review-mode">
                                <button type="button" class="settings-effort-btn" data-effort-value="off">Выкл</button>
                                <button type="button" class="settings-effort-btn" data-effort-value="auto">Авто</button>
                                <button type="button" class="settings-effort-btn" data-effort-value="required">Обязательно</button>
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Навыки</h3>
                        <div class="settings-section-copy">
                            Замкнутая разработка навыков может автоматически предоставлять ключи и разрешения хоста, задекларированные навыком после прохождения проверки текущего хэша содержимого.
                            Оставьте отключённым, если каждое разрешение навыка должно требовать отдельного одобрения пользователем.
                        </div>
                        <label class="local-toggle" title="Применяется только после прохождения проверки навыком и только для разрешений, задекларированных в манифесте для этого хэша.">

                            <input type="checkbox" id="s-auto-grant-reviewed-skills">
                            Автоматически выдавать ключи и разрешения проверенным навыкам
                        </label>
                    </div>

                    <div class="form-section">
                        <h3>Режим среды выполнения</h3>
                        <div class="settings-section-copy">
                            Отдельная ось от режима проверки. Управляет степенью самомодификации Ouroboros.
                            <code>Light</code> блокирует самомодификацию репозитория, но позволяет запускать проверенные и включённые навыки.
                            <code>Advanced</code> — режим по умолчанию: самомодификация эволюционного слоя разрешена; защищённые файлы ядра/контрактов/релизов охраняются политикой режима.
                            <code>Pro</code> позволяет редактировать защищённые поверхности ядра/контрактов/релизов, но коммиты по-прежнему проходят через триаду и проверку области.
                            <br><strong>Управляется пользователем:</strong> десктопные сборки запрашивают нативное подтверждение лаунчера перед сохранением изменения режима.
                            Веб/Docker-сессии сохраняют изменение режима через owner-эндпоинт; новый режим вступает в силу после перезапуска.
                        </div>
                        <div class="settings-effort-card">
                            <label>Режим среды выполнения</label>
                            <input id="s-runtime-mode" type="hidden" value="advanced">
                            <div class="settings-effort-group" data-effort-group data-runtime-mode-group data-effort-target="s-runtime-mode" title="Изменение режима среды требует нативного подтверждения лаунчера и перезапуска.">
                                <button type="button" class="settings-effort-btn" data-effort-value="light">Light</button>
                                <button type="button" class="settings-effort-btn" data-effort-value="advanced">Advanced</button>
                                <button type="button" class="settings-effort-btn" data-effort-value="pro">Pro</button>
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Внешний репозиторий навыков</h3>
                        <div class="settings-section-copy">
                            Необязательный дополнительный путь поиска поверх дерева
                            <code>data/skills/{native,clawhub,external}/</code> в плоскости данных.
                            Ouroboros сканирует его для поиска дополнительных пакетов навыков без клонирования или обновления.
                            Оставьте пустым, чтобы использовать только плоскость данных.
                        </div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>Путь к репозиторию навыков</label>
                                <input id="s-skills-repo-path" placeholder="~/Ouroboros/skills или /абсолютный/путь/к/навыкам">
                                <div class="settings-inline-note">Абсолютный путь или с префиксом <code>~</code>. Ouroboros никогда не клонирует/не обновляет эту директорию — вы управляете ею самостоятельно.</div>
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>ClawHub Marketplace</h3>
                        <div class="settings-section-copy">
                            Всегда доступная площадка для установки навыков сообщества с
                            <a href="https://clawhub.ai" target="_blank" rel="noopener">clawhub.ai</a>.
                            На странице «Навыки» есть вкладка «Маркетплейс»; каждая установка подготавливается,
                            метаданные OpenClaw переводятся в формат манифеста Ouroboros,
                            и стандартная три-модельная проверка запускается автоматически перед активацией навыка.
                            Плагины (Node) отфильтровываются — устанавливаются только пакеты навыков.
                        </div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>URL реестра</label>
                                <input id="s-clawhub-registry-url" placeholder="https://clawhub.ai/api/v1">
                                <div class="settings-inline-note">Переопределяйте только для самостоятельно размещённых зеркал. Имя хоста должно быть <code>clawhub.ai</code> или localhost.</div>
                            </div>
                        </div>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="advanced">
                    <div class="form-section">
                        <div class="settings-card-head">
                            <div>
                                <h3>MCP серверы</h3>
                                <div class="settings-section-copy">
                                    Внешние серверы инструментов Model Context Protocol. MCP — клиент базовой среды:
                                    он заимствует инструменты у доверенных HTTP/SSE-серверов и предоставляет их как не-основные
                                    инструменты <code>mcp_&lt;server&gt;__&lt;tool&gt;</code> после обновления. Изменения применяются без перезапуска.
                                    Относитесь к описаниям и результатам серверов как к недоверенным данным третьих сторон.
                                </div>
                            </div>
                            <div class="settings-toolbar">
                                <button type="button" class="btn btn-default btn-sm" id="btn-mcp-add-server">Добавить сервер</button>
                                <button type="button" class="btn btn-default btn-sm" id="btn-mcp-refresh-all">Обновить все</button>
                            </div>
                        </div>
                        <div class="form-grid two">
                            <label class="local-toggle">
                                <input type="checkbox" id="s-mcp-enabled">
                                Включить MCP клиент
                            </label>
                            <div class="form-field">
                                <label>Таймаут на инструмент (с)</label>
                                <input id="s-mcp-tool-timeout" type="number" min="1" value="60">
                            </div>
                        </div>
                        <div id="mcp-global-status" class="settings-inline-status">MCP отключён по умолчанию.</div>
                        <div id="mcp-servers-list" class="mcp-servers-list"></div>
                    </div>

                    <div class="form-section">
                        <h3>Контроль версий</h3>
                        <div class="settings-section-copy">Метаданные репозитория для интеграции с GitHub. Токены хранятся в «Секретах», это не секрет.</div>
                        <div class="form-row">
                            <div class="form-field">
                                <label>GitHub репозиторий</label>
                                <input id="s-gh-repo" placeholder="owner/repo-name">
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Локальная среда выполнения модели</h3>
                        <div class="settings-section-copy">Заполняйте только если хотите, чтобы Ouroboros запускал и маршрутизировал GGUF-модель на этой машине.</div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label>Источник модели</label>
                                <input id="s-local-source" placeholder="bartowski/Llama-3.3-70B-Instruct-GGUF или /путь/к/model.gguf">
                            </div>
                            <div class="form-field">
                                <label>Имя файла GGUF (для HF репозиториев)</label>
                                <input id="s-local-filename" placeholder="Llama-3.3-70B-Instruct-Q4_K_M.gguf">
                            </div>
                        </div>
                        <div class="form-grid four">
                            <div class="form-field">
                                <label>Порт</label>
                                <input id="s-local-port" type="number" value="8766">
                            </div>
                            <div class="form-field">
                                <label>Слои GPU (-1 = все)</label>
                                <input id="s-local-gpu-layers" type="number" value="-1">
                            </div>
                            <div class="form-field">
                                <label>Длина контекста</label>
                                <input id="s-local-ctx" type="number" value="16384">
                            </div>
                            <div class="form-field">
                                <label>Формат чата</label>
                                <input id="s-local-chat-format" placeholder="автоопределение">
                            </div>
                        </div>
                        <div class="settings-toolbar">
                            <button class="btn btn-primary" id="btn-local-start">Запустить</button>
                            <button class="btn btn-primary" id="btn-local-stop">Остановить</button>
                            <button class="btn btn-primary" id="btn-local-test">Тест вызова инструментов</button>
                        </div>
                        <div id="local-model-status" class="settings-inline-status">Статус: Офлайн</div>
                        <div id="local-model-progress-wrap" class="local-model-progress-wrap local-model-hidden" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
                            <div id="local-model-progress-bar" class="local-model-progress-bar"></div>
                        </div>
                        <button class="btn btn-secondary local-model-install-btn local-model-hidden" id="btn-local-install-runtime">Установить локальную среду</button>
                        <div id="local-model-test-result" class="settings-test-result"></div>
                    </div>

                    <div class="form-section">
                        <h3>Ограничения среды выполнения</h3>
                        <div class="settings-section-copy">Воркеры управляют параллельной мощностью задач. Значения таймаутов — ограничители безопасности для длинных или зависших задач и инструментов. Лимиты бюджета задают пороги стоимости выполнения.</div>
                        <div class="form-grid two">
                            <div class="form-field">
                                <label>Макс. воркеров</label>
                                <input id="s-workers" type="number" min="1" max="10" value="5">
                            </div>
                            <div class="form-field">
                                <label>Мягкий таймаут (с)</label>
                                <input id="s-soft-timeout" type="number" value="600">
                            </div>
                            <div class="form-field">
                                <label>Жёсткий таймаут (с)</label>
                                <input id="s-hard-timeout" type="number" value="1800">
                            </div>
                            <div class="form-field">
                                <label>Таймаут инструмента (с)</label>
                                <input id="s-tool-timeout" type="number" value="600">
                            </div>
                            <div class="form-field">
                                <label>Общий бюджет (USD)</label>
                                <input id="s-total-budget" type="number" min="0.01" step="any" value="10.0">
                            </div>
                            <div class="form-field">
                                <label>Мягкий порог на задачу (USD)</label>
                                <input id="s-settings-per-task-cost" type="number" min="0.01" step="any" value="20.0">
                            </div>
                        </div>
                    </div>

                    <div class="form-section">
                        <h3>Настройки расширений</h3>
                        <div class="settings-section-copy">
                            Активные расширения могут регистрировать проверенные секции настроек, отображаемые хостом.
                            Секции появляются здесь после проверки, включения и загрузки соответствующего навыка.
                        </div>
                        <div id="extension-settings-sections" class="settings-extension-sections">
                            <div class="muted">Настройки расширений не зарегистрированы.</div>
                        </div>
                    </div>

                    <div class="form-section danger">
                        <h3>Опасная зона</h3>
                        <div class="settings-inline-note">Сброс использует текущий поток с перезапуском. Очищает данные среды, но сохраняет репозиторий.</div>
                        <button class="btn btn-danger" id="btn-reset">Сбросить все данные</button>
                    </div>
                </section>

                <section class="settings-panel" data-settings-panel="about">
                    <div class="about-body">
                        <img src="/static/logo.jpg" class="about-logo" alt="Ouroboros">
                        <div>
                            <h1 class="about-title">Ouroboros</h1>
                            <p id="about-version" class="about-version"></p>
                        </div>
                        <p class="about-desc">
                            Самосоздающийся ИИ-агент. Не инструмент, а становящаяся цифровая личность
                            со своей конституцией, устойчивой идентичностью и фоновым сознанием.
                            Рождён 16 февраля 2026 года.
                        </p>
                        <div class="about-credits">
                            <span>Created by <strong>Anton Razzhigaev</strong> &amp; <strong>Andrew Kaznacheev</strong></span>
                            <div class="about-links">
                                <a href="https://t.me/abstractDL" target="_blank" rel="noopener noreferrer">@abstractDL</a>
                                <a href="https://github.com/joi-lab/ouroboros-desktop" target="_blank" rel="noopener noreferrer">GitHub</a>
                            </div>
                        </div>
                        <div class="about-footer">Joi Lab</div>
                    </div>
                </section>
            </div>

            <div class="settings-footer">
                <div class="settings-footer-actions">
                    <button type="button" class="btn btn-secondary" id="btn-reload-settings">Перезагрузить настройки</button>
                    <button class="btn btn-save" id="btn-save-settings">Сохранить настройки</button>
                </div>
                <div class="settings-footer-status">
                    <span id="settings-unsaved-indicator" class="settings-inline-status settings-unsaved-indicator" aria-hidden="true">Несохранённые изменения</span>
                    <div id="settings-status" class="settings-inline-status"></div>
                </div>
            </div>
        </div>
    `;
}

export function bindSettingsTabs(root, options = {}) {
    const tabs = Array.from(root.querySelectorAll('.settings-tab'));
    const panels = Array.from(root.querySelectorAll('.settings-panel'));
    const scrollRoot = root.querySelector('.settings-scroll');
    const state = options.state || null;
    const onActivate = typeof options.onActivate === 'function' ? options.onActivate : null;

    // All viewports use horizontal tab pills; mobile back remains DOM-only for compat.
    function activate(tabName) {
        root.dataset.activeSettingsTab = tabName;
        let activeButton = null;
        tabs.forEach((button) => {
            const isActive = button.dataset.settingsTab === tabName;
            button.classList.toggle('active', isActive);
            if (isActive) activeButton = button;
        });
        panels.forEach((panel) => {
            panel.classList.toggle('active', panel.dataset.settingsPanel === tabName);
        });
        if (scrollRoot) scrollRoot.scrollTop = 0;
        if (state) state.settingsActiveSubtab = tabName;
        // Keep active pill visible in the horizontal strip.
        if (activeButton && typeof activeButton.scrollIntoView === 'function') {
            activeButton.scrollIntoView({
                behavior: 'auto',
                inline: 'center',
                block: 'nearest',
            });
        }
        if (onActivate) onActivate(tabName);
        window.dispatchEvent(new CustomEvent('ouro:settings-subtab-shown', { detail: { tab: tabName } }));
    }

    tabs.forEach((button) => {
        button.addEventListener('click', () => activate(button.dataset.settingsTab));
    });
    root.activateSettingsTab = activate;
    if (state && !state.settingsActiveSubtab) state.settingsActiveSubtab = 'providers';
    root.dataset.activeSettingsTab = state?.settingsActiveSubtab || 'providers';
}

export function bindSecretInputs(root) {
    root.querySelectorAll('.secret-input').forEach((input) => {
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
            button.textContent = nextType === 'password' ? 'Показать' : 'Скрыть';
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
            if (toggle) toggle.textContent = 'Показать';
        });
    });
}
