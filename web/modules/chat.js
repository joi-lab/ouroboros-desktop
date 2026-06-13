import { apiFetch } from './api_client.js';
import {
    getLogTaskGroupId,
    isGroupedTaskEvent,
    normalizeLogTs,
    summarizeChatLiveEvent,
} from './log_events.js';
import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { showToast } from './toast.js';
import { escapeHtmlText as escapeHtml, escapeHtmlAttr, formatUsdWhole, renderMarkdown } from './utils.js';

const CHAT_STORAGE_KEY = 'ouro_chat';
const CHAT_INPUT_HISTORY_KEY = 'ouro_chat_input_history';
const CHAT_SESSION_ID_KEY = 'ouro_chat_session_id';
const PLAN_PREFIX = 'Please do multi-model planning (plan_task tool) and web-search before answering or starting this task:\n\n';
const MAX_PENDING_ATTACHMENTS = 10;
const MAX_ATTACHMENT_FILE_BYTES = 50 * 1024 * 1024;
const MAX_PENDING_ATTACHMENT_BYTES = 100 * 1024 * 1024;

function getOrCreateChatSessionId() {
    try {
        const existing = sessionStorage.getItem(CHAT_SESSION_ID_KEY);
        if (existing) return existing;
        const created = (globalThis.crypto && typeof crypto.randomUUID === 'function')
            ? crypto.randomUUID()
            : `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        sessionStorage.setItem(CHAT_SESSION_ID_KEY, created);
        return created;
    } catch {
        return `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }
}

function loadInputHistory() {
    try {
        const raw = JSON.parse(sessionStorage.getItem(CHAT_INPUT_HISTORY_KEY) || '[]');
        return Array.isArray(raw) ? raw.filter(Boolean).slice(-50) : [];
    } catch {
        return [];
    }
}

function saveInputHistory(entries) {
    try {
        sessionStorage.setItem(CHAT_INPUT_HISTORY_KEY, JSON.stringify(entries.slice(-50)));
    } catch {}
}

export function initChat({ ws, state, updateUnreadBadge, openSettingsTab, openDashboardTab }) {
    const container = document.getElementById('content');
    const chatSessionId = getOrCreateChatSessionId();

    const page = document.createElement('div');
    page.id = 'page-chat';
    page.className = 'page active';
    page.innerHTML = `
        ${renderPageHeader({
            title: 'Чат',
            icon: PAGE_ICONS.chat,
            variant: 'overlay',
            className: 'chat-page-header',
            actionsHtml: `
                <div class="chat-action-menu" id="chat-action-menu">
                    <span id="chat-status" class="status-badge offline">Подключение...</span>
                    <button class="chat-action-trigger" id="chat-action-trigger" type="button" title="Действия чата" aria-label="Действия чата" aria-expanded="false" aria-haspopup="menu">
                        <img class="chat-action-trigger-icon" src="/static/icons/settings-cog.svg" alt="">
                    </button>
                    <div class="chat-header-actions chat-action-dropdown" id="chat-header-actions" role="menu">
                        <button class="chat-header-btn" type="button" data-chat-command="evolve" role="menuitem" title="Toggle evolution mode"><span class="chat-action-switch" aria-hidden="true"></span><span>Эволюция</span></button>
                        <button class="chat-header-btn" type="button" data-chat-command="review" role="menuitem" title="Run review now"><span class="chat-action-switch" aria-hidden="true"></span><span>Ревью кода</span></button>
                        <button class="chat-header-btn" type="button" data-chat-command="bg" role="menuitem" title="Toggle background consciousness"><span class="chat-action-switch" aria-hidden="true"></span><span>Фоновое сознание</span></button>
                        <button class="chat-header-btn" type="button" data-chat-command="restart" role="menuitem" title="Restart agent"><span class="chat-action-icon" aria-hidden="true"><img src="/static/icons/restart.svg" alt=""></span><span>Перезапустить чат</span></button>
                        <button class="chat-header-btn danger" type="button" data-chat-command="panic" role="menuitem" title="Stop all workers"><span class="chat-action-icon" aria-hidden="true"><img src="/static/icons/power.svg" alt=""></span><span>Экстренное отключение</span></button>
                    </div>
                </div>
            `,
        })}
        <div id="chat-messages"></div>
        <div id="chat-input-area">
            <div id="chat-attachment-preview" class="chat-attachment-preview"></div>
            <div class="chat-input-wrap">
                <button class="chat-attach-btn" id="chat-attach" type="button" title="Прикрепить файл">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                    <span class="chat-attach-label">Прикрепить</span>
                </button>
                <input type="file" id="chat-file-input" class="chat-file-input-hidden" accept="*/*" multiple>
                <textarea id="chat-input" placeholder="Сообщение Ouroboros..." rows="1" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
                <div class="chat-send-group">
                    <button class="chat-send-inline" id="chat-send" title="Отправить сообщение">Отправить</button>
                    <button class="chat-send-chevron" id="chat-send-chevron" type="button" title="Другие варианты отправки" aria-label="Другие варианты отправки">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="chat-send-dropdown" id="chat-send-dropdown" role="menu">
                        <button class="chat-send-dropdown-item" id="chat-dropdown-send" role="menuitem">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                            Отправить
                        </button>
                        <button class="chat-send-dropdown-item" id="chat-dropdown-plan" role="menuitem">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/></svg>
                            Планировать
                        </button>
                    </div>
                </div>
            </div>
        </div>
    `;
    container.appendChild(page);

    const messagesDiv = document.getElementById('chat-messages');
    const input = document.getElementById('chat-input');
    const inputArea = document.getElementById('chat-input-area');
    const sendBtn = document.getElementById('chat-send');
    const chevronBtn = document.getElementById('chat-send-chevron');
    const sendDropdown = document.getElementById('chat-send-dropdown');
    const dropdownSend = document.getElementById('chat-dropdown-send');
    const dropdownPlan = document.getElementById('chat-dropdown-plan');
    const statusBadge = document.getElementById('chat-status');
    const actionMenu = document.getElementById('chat-action-menu');
    const actionTrigger = document.getElementById('chat-action-trigger');
    const headerActions = document.getElementById('chat-header-actions');
    const budgetPill = document.getElementById('chat-budget-pill');
    const attachBtn = document.getElementById('chat-attach');
    const fileInput = document.getElementById('chat-file-input');
    const attachmentPreview = document.getElementById('chat-attachment-preview');
    let pendingAttachments = [];
    let attachmentsUploading = false;

    function pendingAttachmentBytes(items = pendingAttachments) {
        return items.reduce((total, item) => total + Number(item.file?.size || 0), 0);
    }

    function updateAttachmentPreview() {
        if (!pendingAttachments.length) {
            attachmentPreview.classList.remove('visible');
            attachmentPreview.innerHTML = '';
            requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
            return;
        }
        attachmentPreview.classList.add('visible');
        attachmentPreview.innerHTML = pendingAttachments.map((item) => `
            <span class="attach-badge" data-attachment-id="${escapeHtmlAttr(item.id)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>
                <span class="attach-name" title="${escapeHtmlAttr(item.display_name)}">${escapeHtml(item.display_name)}</span>
                <button class="attach-remove" type="button" title="Remove" aria-label="Remove attachment ${escapeHtmlAttr(item.display_name)}" data-attachment-remove="${escapeHtmlAttr(item.id)}" ${attachmentsUploading ? 'disabled aria-disabled="true"' : ''}>×</button>
            </span>
        `).join('');
        requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
        attachmentPreview.querySelectorAll('[data-attachment-remove]').forEach((button) => {
            button.addEventListener('click', () => {
                if (attachmentsUploading) return;
                const removeId = button.getAttribute('data-attachment-remove') || '';
                pendingAttachments = pendingAttachments.filter((item) => item.id !== removeId);
                updateAttachmentPreview();
            });
        });
    }

    // Shared paperclip/paste stager; upload still happens only on Send.
    function stagePendingFiles(files) {
        const incoming = Array.from(files || []).filter(Boolean);
        if (!incoming.length) return;
        if (attachmentsUploading) {
            showToast('Wait for the current upload to finish before changing attachments.', 'error');
            return;
        }
        if (pendingAttachments.length + incoming.length > MAX_PENDING_ATTACHMENTS) {
            showToast(`Attach up to ${MAX_PENDING_ATTACHMENTS} files per message.`, 'error');
            return;
        }
        const oversized = incoming.find((file) => Number(file.size || 0) > MAX_ATTACHMENT_FILE_BYTES);
        if (oversized) {
            showToast(`Each attachment must be ${Math.round(MAX_ATTACHMENT_FILE_BYTES / (1024 * 1024))} MB or smaller.`, 'error');
            return;
        }
        const incomingBytes = incoming.reduce((total, file) => total + Number(file.size || 0), 0);
        if (pendingAttachmentBytes() + incomingBytes > MAX_PENDING_ATTACHMENT_BYTES) {
            const limitMb = Math.round(MAX_PENDING_ATTACHMENT_BYTES / (1024 * 1024));
            showToast(`Attachments are limited to ${limitMb} MB total per message.`, 'error');
            return;
        }
        pendingAttachments = pendingAttachments.concat(incoming.map((file) => ({
            id: (globalThis.crypto && typeof crypto.randomUUID === 'function')
                ? crypto.randomUUID()
                : `attachment-${Date.now()}-${Math.random().toString(16).slice(2)}`,
            file,
            display_name: file.name || 'upload',
        })));
        updateAttachmentPreview();
    }

    async function cleanupUploadedAttachments(uploaded) {
        const filenames = uploaded
            .map((item) => item.filename)
            .filter(Boolean);
        if (!filenames.length) return;
        const results = await Promise.allSettled(filenames.map(async (filename) => {
            const resp = await apiFetch('/api/chat/upload', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename }),
            });
            if (!resp.ok) throw new Error(`DELETE ${filename} failed with HTTP ${resp.status}`);
        }));
        const failed = results.filter((result) => result.status === 'rejected');
        if (failed.length) {
            console.warn('Failed to clean up uploaded chat attachments after send failure', failed);
        }
    }

    function setAttachmentUploadState(uploading) {
        attachmentsUploading = uploading;
        attachBtn.disabled = uploading;
        attachBtn.classList.toggle('uploading', uploading);
        fileInput.disabled = uploading;
        input.disabled = uploading;
        updateAttachmentPreview();
    }

    attachBtn.addEventListener('click', () => fileInput.click());

    // Local-only staging avoids orphan uploads and fast-send races.
    fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files || []);
        fileInput.value = '';
        stagePendingFiles(files);
    });

    // Image paste uses the same stager; only image matches call preventDefault().
    // Timestamped names keep repeated clipboard images distinct.
    input.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        const pastedImages = [];
        for (let i = 0; i < items.length; i += 1) {
            const item = items[i];
            if (item && item.kind === 'file' && typeof item.type === 'string' && item.type.startsWith('image/')) {
                const blob = item.getAsFile();
                if (!blob) continue;
                const ext = (item.type.split('/')[1] || 'png').split(';')[0].trim() || 'png';
                const ts = Date.now() + i;
                const safeBlob = blob instanceof File
                    ? new File([blob], `clipboard-${ts}.${ext}`, { type: blob.type })
                    : new File([blob], `clipboard-${ts}.${ext}`, { type: item.type });
                pastedImages.push(safeBlob);
            }
        }
        if (!pastedImages.length) return;
        e.preventDefault();
        stagePendingFiles(pastedImages);
    });

    // Pass 1 builds live cards in memory; pass 2 inserts them in transcript order.
    let _syncPass1Active = false;

    const persistedHistory = [];
    const seenMessageKeys = new Set();
    const messageKeyOrder = [];
    const pendingUserBubbles = new Map();
    const inputHistory = loadInputHistory();
    let inputHistoryIndex = inputHistory.length;
    let inputDraft = '';
    let historyLoaded = false;
    let inputHistorySeededFromServer = false; // set true only after a successful server-side recall seed
    let historySyncPromise = null;
    let welcomeShown = false;
    const liveCardRecords = new Map();
    const taskUiStates = new Map();
    // Finished task ids hidden from routine syncs until reload/reconnect rebuilds history.
    const retiredTaskIds = new Set();
    let activeLiveGroupId = '';
    let historySyncTimer = null;
    let pendingReconnectSync = false;  // Set when a fromReconnect sync arrives while one is already in-flight.
    let pendingReconnectBannerText = readPendingReconnectBanner();

    function buildMessageKey(role, text, timestamp, opts = {}) {
        if (opts.clientMessageId) return `client|${opts.clientMessageId}`;
        if (role !== 'user' && !opts.isProgress && opts.taskId) {
            return [
                'task',
                role,
                opts.systemType || '',
                opts.source || '',
                opts.taskId,
                text,
            ].join('|');
        }
        if (!timestamp) return '';
        return [
            role,
            opts.isProgress ? '1' : '0',
            opts.systemType || '',
            opts.source || '',
            opts.senderLabel || '',
            opts.senderSessionId || '',
            opts.taskId || '',
            timestamp,
            text,
        ].join('|');
    }

    function reconnectBannerText(reason = '') {
        if (reason === 'sha-change') return '♻️ Перезапущен';
        if (reason) return '♻️ Соединение установлено';
        return '';
    }

    function readPendingReconnectBanner() {
        try {
            const url = new URL(window.location.href);
            return reconnectBannerText(url.searchParams.get('_ouro_reason') || '');
        } catch {
            return '';
        }
    }

    function clearPendingReconnectBanner() {
        try {
            const url = new URL(window.location.href);
            if (!url.searchParams.has('_ouro_reason') && !url.searchParams.has('_ouro_refresh')) return;
            url.searchParams.delete('_ouro_reason');
            url.searchParams.delete('_ouro_refresh');
            window.history.replaceState({}, '', url);
        } catch {}
    }

    function rememberMessageKey(key) {
        if (!key || seenMessageKeys.has(key)) return;
        seenMessageKeys.add(key);
        messageKeyOrder.push(key);
        if (messageKeyOrder.length > 2000) {
            const oldest = messageKeyOrder.shift();
            if (oldest) seenMessageKeys.delete(oldest);
        }
    }

    function formatMsgTime(isoStr) {
        if (!isoStr) return null;
        try {
            const d = new Date(isoStr);
            if (isNaN(d)) return null;
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const hhmm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
            const months = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
            const todayStr = now.toDateString();
            const yesterday = new Date(now);
            yesterday.setDate(now.getDate() - 1);
            let short;
            if (d.toDateString() === todayStr) short = hhmm;
            else if (d.toDateString() === yesterday.toDateString()) short = `Вчера, ${hhmm}`;
            else short = `${months[d.getMonth()]} ${d.getDate()}, ${hhmm}`;
            const full = `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} в ${hhmm}`;
            return { short, full };
        } catch {
            return null;
        }
    }

    function getSenderLabel(role, isProgress = false, systemType = '', opts = {}) {
        if (role === 'user') {
            if (opts.source === 'telegram') return opts.senderLabel || 'Telegram';
            if (opts.senderSessionId && opts.senderSessionId !== chatSessionId) {
                return `WebUI (${opts.senderSessionId.slice(0, 8)})`;
            }
            return opts.senderLabel || 'Вы';
        }
        if (role === 'system') {
            if (systemType === 'task_summary') return '📋 Сводка задачи';
            if (systemType === 'skill_review') return '📋 Ревью навыка';
            return '📋 Система';
        }
        if (isProgress) return '💬 Мысль';
        return 'Ouroboros';
    }

    function summarizeSkillReviewMessage(text) {
        const raw = String(text || '');
        const lines = raw.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
        const headline = lines[0] || 'Skill review';
        const hashLine = lines.find((line) => line.startsWith('content_hash=')) || '';
        const reviewersLine = lines.find((line) => line.startsWith('Reviewers:')) || '';
        const findingsLine = lines.find((line) => /^##\s+Findings/.test(line)) || '';
        const meta = [hashLine, reviewersLine.replace(/^Reviewers:\s*/, ''), findingsLine.replace(/^##\s*/, '')]
            .filter(Boolean)
            .map((line) => escapeHtml(line.length > 140 ? `${line.slice(0, 137)}...` : line))
            .join(' · ');
        return {
            headline: escapeHtml(headline.replace(/^#+\s*/, '')),
            meta,
        };
    }

    function renderSkillReviewDisclosure(text) {
        const summary = summarizeSkillReviewMessage(text);
        return `
            <div class="skill-review-disclosure" data-skill-review-disclosure data-expanded="0">
                <button type="button" class="skill-review-summary-button" data-skill-review-toggle aria-expanded="false">
                    <span class="skill-review-summary-main">${summary.headline}</span>
                    <span class="skill-review-summary-side">
                        <span class="skill-review-meta">${summary.meta}</span>
                        <span class="skill-review-toggle-label">Показать ревью</span>
                    </span>
                </button>
                <div class="skill-review-full" data-skill-review-full hidden>${renderMarkdown(text)}</div>
            </div>
        `;
    }

    function setStatus(kind, text) {
        if (!statusBadge) return;
        statusBadge.className = `status-badge ${kind}`;
        statusBadge.textContent = text;
    }

    function syncHeaderControlState(data) {
        headerActions?.querySelectorAll('[data-chat-command]').forEach((button) => {
            const cmd = button.dataset.chatCommand;
            if (cmd === 'evolve') {
                button.classList.toggle('on', !!data?.evolution_enabled);
                if (data?.evolution_state?.detail) button.title = data.evolution_state.detail;
            } else if (cmd === 'bg') {
                button.classList.toggle('on', !!data?.bg_consciousness_enabled);
                if (data?.bg_consciousness_state?.detail) button.title = data.bg_consciousness_state.detail;
            }
        });
        const spent = data?.spent_usd || 0;
        const limit = data?.budget_limit || 10;
        const budgetLabel = typeof data?.budget_text === 'string'
            ? data.budget_text
            : `${formatUsdWhole(spent)} / ${formatUsdWhole(limit)}`;
        const budgetText = document.getElementById('chat-budget-text');
        const budgetFill = document.getElementById('chat-budget-bar-fill');
        if (budgetText) budgetText.textContent = budgetLabel;
        if (budgetFill) budgetFill.style.width = `${Math.min(100, (spent / limit) * 100)}%`;
    }

    async function refreshHeaderControlState(force = false) {
        if (!force && state.activePage !== 'chat') return;
        try {
            const resp = await apiFetch('/api/state', { cache: 'no-store' });
            if (!resp.ok) return;
            syncHeaderControlState(await resp.json());
        } catch {}
    }

    function persistVisibleHistory() {
        try {
            sessionStorage.setItem(CHAT_STORAGE_KEY, JSON.stringify(persistedHistory.slice(-200)));
        } catch {}
    }

    const NEAR_BOTTOM_THRESHOLD_PX = 160;
    const REATTACH_STICKY_THRESHOLD_PX = 48;
    let shouldAutoStickToBottom = true;
    let lastMessagesScrollTop = 0;
    let suppressStickyDetection = false;

    function isNearBottom(threshold = NEAR_BOTTOM_THRESHOLD_PX) {
        const remaining = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
        return remaining <= threshold;
    }

    function setProgrammaticScrollTop(nextScrollTop) {
        suppressStickyDetection = true;
        messagesDiv.scrollTop = nextScrollTop;
        requestAnimationFrame(() => {
            suppressStickyDetection = false;
            lastMessagesScrollTop = messagesDiv.scrollTop;
        });
    }

    function setStickyByUserScroll() {
        if (suppressStickyDetection) return;
        const current = messagesDiv.scrollTop;
        if (current < lastMessagesScrollTop - 2) shouldAutoStickToBottom = false;
        else if (isNearBottom(REATTACH_STICKY_THRESHOLD_PX)) shouldAutoStickToBottom = true;
        lastMessagesScrollTop = current;
    }

    function insertMessageNode(node, options = {}) {
        if (!node) return;
        const shouldStick = Boolean(options.forceStick) || (shouldAutoStickToBottom && isNearBottom());
        if (node.parentNode === messagesDiv) {
            if (shouldStick) setProgrammaticScrollTop(messagesDiv.scrollHeight);
            return;
        }
        const typing = document.getElementById('typing-indicator');
        if (typing && typing.parentNode === messagesDiv) messagesDiv.insertBefore(node, typing);
        else messagesDiv.appendChild(node);
        if (shouldStick) setProgrammaticScrollTop(messagesDiv.scrollHeight);
    }

    function shouldAlwaysShowTaskCard(taskId = '') {
        return taskId === 'bg-consciousness';
    }

    function isTerminalTaskPhase(phase = '', terminal = false) {
        return Boolean(terminal) || ['done', 'lifecycle_error'].includes(phase);
    }

    function createTaskUiState(taskId) {
        if (!taskId) return null;
        const taskState = {
            taskId,
            toolCalls: 0,
            forceCard: false,
            cardVisible: false,
            completed: false,
            completedPhase: '',
            bufferedLiveUpdates: [],
            cleanupTimer: null,
        };
        taskUiStates.set(taskId, taskState);
        return taskState;
    }

    function getTaskUiState(taskId = '', createIfMissing = true) {
        if (!taskId) return null;
        if (taskUiStates.has(taskId)) return taskUiStates.get(taskId);
        return createIfMissing ? createTaskUiState(taskId) : null;
    }

    function scheduleTaskUiCleanup(taskState, delayMs = 120000) {
        if (!taskState) return;
        if (taskState.cleanupTimer) clearTimeout(taskState.cleanupTimer);
        taskState.cleanupTimer = setTimeout(() => {
            taskUiStates.delete(taskState.taskId);
            // Keep the finished card interactive, but mark it retired so routine
            // syncs do not rebuild duplicates. Reload/reconnect clears this set.
            if (!REUSABLE_TASK_IDS.has(taskState.taskId) && taskState.taskId !== '') {
                retiredTaskIds.add(taskState.taskId);
            }
        }, delayMs);
    }

    function bufferLiveUpdate(taskState, summary, ts, dedupeKey = '') {
        if (!taskState || !summary) return;
        taskState.bufferedLiveUpdates.push({
            summary,
            ts,
            dedupeKey: dedupeKey || summary.dedupeKey || '',
        });

    }

    function revealBufferedCardIfNeeded(taskState, { suppressDomInsert = false } = {}) {
        if (!taskState || taskState.cardVisible) return;
        if (!(taskState.forceCard || taskState.toolCalls > 0 || shouldAlwaysShowTaskCard(taskState.taskId))) {
            return;
        }
        taskState.cardVisible = true;
        activeLiveGroupId = taskState.taskId;
        const record = getLiveCardRecord(taskState.taskId);
        ensureLiveCardVisible(record, { suppressDomInsert });
        const bufferedUpdates = [...taskState.bufferedLiveUpdates];
        taskState.bufferedLiveUpdates = [];
        for (const update of bufferedUpdates) {
            applyLiveCardState(update.summary, taskState.taskId, update.ts, update.dedupeKey, { suppressDomInsert });
        }
        if (taskState.completed) {
            finishLiveCard(taskState.taskId, taskState.completedPhase || 'done');
        }
    }

    function markTaskToolCall(taskId, count = 1, minimumOnly = false) {
        const taskState = getTaskUiState(taskId, true);
        if (!taskState) return null;
        const safeCount = Math.max(0, Number(count) || 0);
        if (minimumOnly) {
            taskState.toolCalls = Math.max(taskState.toolCalls, safeCount);
        } else {
            taskState.toolCalls += safeCount;
        }
        revealBufferedCardIfNeeded(taskState);
        return taskState;
    }

    function forceTaskCard(taskId) {
        const taskState = getTaskUiState(taskId, true);
        if (!taskState) return null;
        taskState.forceCard = true;
        revealBufferedCardIfNeeded(taskState);
        return taskState;
    }

    function markAssistantReply(taskId = '') {
        const resolvedTaskId = taskId || '';
        if (!resolvedTaskId) return;
        const taskState = getTaskUiState(resolvedTaskId, false);
        if (!taskState) return;
        taskState.completed = true;
        taskState.completedPhase = taskState.completedPhase || 'done';
        if (!taskState.cardVisible) {
            scheduleTaskUiCleanup(taskState, 30000);
            return;
        }
        scheduleTaskUiCleanup(taskState);
    }

    function markTaskComplete(taskId = '', phase = '') {
        const taskState = getTaskUiState(taskId, false);
        if (!taskState) return;
        taskState.completed = true;
        if (phase) taskState.completedPhase = phase;
    }

    // Logical slots that may host multiple independent cycles.
    const REUSABLE_TASK_IDS = new Set(['bg-consciousness', 'active']);

    function queueTaskLiveUpdate(summary, taskId, ts, dedupeKey = '') {
        const resolvedTaskId = taskId || activeLiveGroupId || '';
        if (!resolvedTaskId) return;
        const taskState = getTaskUiState(resolvedTaskId, true);
        if (!taskState) return;
        if (taskState.completed && !isTerminalTaskPhase(summary.phase || '', summary.terminal)) {
            // A non-terminal event on a reusable id starts a fresh visible cycle.
            if (REUSABLE_TASK_IDS.has(resolvedTaskId)) {
                if (taskState.cleanupTimer) clearTimeout(taskState.cleanupTimer);
                taskState.completed = false;
                taskState.completedPhase = '';
                taskState.cardVisible = false;
                taskState.bufferedLiveUpdates = [];
                taskState.toolCalls = 0;
                taskState.forceCard = false;
                const oldRec = liveCardRecords.get(resolvedTaskId);
                if (oldRec) {
                    oldRec.root?.remove();
                    liveCardRecords.delete(resolvedTaskId);
                }
                retiredTaskIds.delete(resolvedTaskId);
            } else {
                return;
            }
        }
        if (summary.phase === 'error' || summary.phase === 'timeout') {
            taskState.forceCard = true;
        }
        if (!taskState.cardVisible) {
            bufferLiveUpdate(taskState, summary, ts, dedupeKey);
            revealBufferedCardIfNeeded(taskState);
            return;
        }
        applyLiveCardState(summary, resolvedTaskId, ts, dedupeKey);
    }

    function createLiveCardRecord(groupId = '', options = {}) {
        const normalizedGroupId = groupId || `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const timelineId = `chat-live-timeline-${normalizedGroupId.replace(/[^A-Za-z0-9_-]/g, '-')}`;
        const root = document.createElement('div');
        root.className = 'chat-live-card';
        if (options.isSubagent) {
            root.classList.add('subagent');
            root.dataset.subagent = '1';
            root.dataset.parentTaskId = String(options.parentGroupId || '');
        }
        root.dataset.finished = '0';
        root.dataset.expanded = '0';
        root.innerHTML = `
            <button type="button" class="chat-live-summary-button" data-live-summary-button aria-expanded="false" aria-controls="${escapeHtmlAttr(timelineId)}">
                <div class="chat-live-summary">
                    <div class="chat-live-summary-main">
                        <span class="chat-live-phase working" data-live-phase>Работает</span>
                        <div class="chat-live-typing" data-live-typing aria-hidden="true">
                            <span></span><span></span><span></span>
                        </div>
                        <span class="chat-live-title" data-live-title>Ожидание работы</span>
                    </div>
                    <div class="chat-live-summary-side">
                        <span class="chat-live-count" data-live-count hidden>2 заметки</span>
                        <span class="chat-live-toggle" data-live-toggle>Показать детали</span>
                        <svg class="chat-live-chevron" width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                            <path d="M5 7.5 10 12.5 15 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"></path>
                        </svg>
                    </div>
                </div>
                <div class="chat-live-meta" data-live-meta></div>
            </button>
            <div class="chat-live-timeline" data-live-timeline id="${escapeHtmlAttr(timelineId)}"></div>
        `;
        const record = {
            groupId: normalizedGroupId,
            root,
            summaryButtonEl: root.querySelector('[data-live-summary-button]'),
            phaseEl: root.querySelector('[data-live-phase]'),
            inlineTypingEl: root.querySelector('[data-live-typing]'),
            titleEl: root.querySelector('[data-live-title]'),
            countEl: root.querySelector('[data-live-count]'),
            metaEl: root.querySelector('[data-live-meta]'),
            toggleEl: root.querySelector('[data-live-toggle]'),
            timelineEl: root.querySelector('[data-live-timeline]'),
            updates: 0,
            finished: false,
            items: [],
            lastHumanHeadline: '',
            expandedLineKeys: new Set(),
            isSubagent: Boolean(options.isSubagent),
            parentGroupId: String(options.parentGroupId || ''),
            subagentRole: String(options.role || ''),
            subagentsEl: null,
            // Hidden-page layout sync is deferred until page/visibility returns.
            _needsLayoutSync: false,
        };
        record.summaryButtonEl?.addEventListener('click', () => {
            setLiveCardExpanded(record, record.root.dataset.expanded !== '1');
        });
        record.timelineEl?.addEventListener('click', (event) => {
            const button = event.target.closest('[data-live-line-toggle]');
            if (!button) return;
            const lineKey = button.dataset.liveLineToggle || '';
            if (!lineKey) return;
            if (record.expandedLineKeys.has(lineKey)) record.expandedLineKeys.delete(lineKey);
            else record.expandedLineKeys.add(lineKey);
            renderLiveCardTimeline(record);
            syncLiveCardLayout(record);
        });
        liveCardRecords.set(normalizedGroupId, record);
        resetLiveCardRecord(record);
        return record;
    }

    function getLiveCardRecord(groupId = '') {
        const normalizedGroupId = groupId || activeLiveGroupId || 'chat';
        return liveCardRecords.get(normalizedGroupId) || createLiveCardRecord(normalizedGroupId);
    }

    function ensureSubagentContainer(parentId = '') {
        if (!parentId) return null;
        const parentRecord = getLiveCardRecord(parentId);
        if (!parentRecord.subagentsEl) {
            const container = document.createElement('div');
            container.className = 'chat-subagents';
            container.dataset.subagentsFor = parentId;
            parentRecord.subagentsEl = container;
            parentRecord.timelineEl?.insertAdjacentElement('afterend', container);
        }
        return parentRecord.subagentsEl;
    }

    function getSubagentCardRecord(childId = '', parentId = '', role = '') {
        if (!childId || !parentId) return null;
        const existing = liveCardRecords.get(childId);
        const record = existing || createLiveCardRecord(childId, {
            isSubagent: true,
            parentGroupId: parentId,
            role,
        });
        record.isSubagent = true;
        record.parentGroupId = parentId;
        record.subagentRole = role || record.subagentRole || '';
        record.root.classList.add('subagent');
        record.root.dataset.subagent = '1';
        record.root.dataset.parentTaskId = parentId;
        const container = ensureSubagentContainer(parentId);
        if (container && record.root.parentNode !== container) {
            container.appendChild(record.root);
        }
        return record;
    }

    function setLiveCardTypingVisible(record, visible) {
        if (!record?.inlineTypingEl) return;
        record.inlineTypingEl.style.display = visible ? '' : 'none';
    }

    function resetLiveCardRecord(record) {
        record.updates = 0;
        record.finished = false;
        record.items = [];
        record.lastHumanHeadline = '';
        record.expandedLineKeys.clear();
        record.titleEl.textContent = 'Работает...';
        record.phaseEl.dataset.phase = 'working';
        record.phaseEl.textContent = 'Работает';
        record.phaseEl.className = 'chat-live-phase working';
        record.countEl.hidden = true;
        record.countEl.textContent = '0 заметок';
        record.metaEl.innerHTML = '';
        record.timelineEl.innerHTML = '';
        record.root.style.minHeight = '';
        record.root.dataset.finished = '0';
        setLiveCardTypingVisible(record, true);
        setLiveCardExpanded(record, false);
    }

    function ensureLiveCardVisible(record, { suppressDomInsert = false } = {}) {
        if (record?.isSubagent && record.parentGroupId) {
            if (!suppressDomInsert && !_syncPass1Active) {
                const parentRecord = getLiveCardRecord(record.parentGroupId);
                insertMessageNode(parentRecord.root);
                const container = ensureSubagentContainer(record.parentGroupId);
                if (container && record.root.parentNode !== container) {
                    container.appendChild(record.root);
                }
            }
            return;
        }
        if (!record.isSubagent && !suppressDomInsert && !_syncPass1Active) insertMessageNode(record.root);
    }

    function formatLiveCardPhaseLabel(phase) {
        if (phase === 'thinking') return 'Думает';
        if (phase === 'working') return 'Работает';
        if (phase === 'done') return 'Готово';
        if (phase === 'warn') return 'Внимание';
        if (phase === 'error' || phase === 'timeout') return 'Ошибка';
        if (!phase) return 'Работает';
        return phase.charAt(0).toUpperCase() + phase.slice(1);
    }

    function setLiveCardExpanded(record, expanded) {
        if (!record?.root) return;
        record.root.dataset.expanded = expanded ? '1' : '0';
        // Re-apply timeline visibility explicitly so viewport switches
        // (mobile -> desktop and back) cannot leave stale display state.
        if (record.timelineEl) {
            record.timelineEl.style.display = expanded ? 'flex' : 'none';
        }
        syncLiveCardToggle(record);
        if (record.root.isConnected) {
            requestAnimationFrame(() => syncLiveCardLayout(record));
        }
    }

    function isLiveLineExpandable(item) {
        return Boolean(
            (item.fullHeadline && item.fullHeadline !== item.headline)
            || (item.fullBody && item.fullBody !== item.body)
        );
    }

    function syncLiveCardToggle(record) {
        if (!record?.toggleEl) return;
        const expanded = record.root.dataset.expanded === '1';
        record.toggleEl.textContent = expanded ? 'Скрыть детали' : 'Показать детали';
        record.summaryButtonEl?.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }

    const TIMELINE_MAX_HEIGHT = 420;

    function syncLiveCardLayout(record) {
        if (!record?.root || !record.summaryButtonEl) return;
        // Hidden SPA/browser tabs report zero geometry; defer to avoid collapsed cards.
        if (!record.root.closest('.page.active') || document.hidden) {
            record._needsLayoutSync = true;
            return;
        }
        record._needsLayoutSync = false;
        const summaryHeight = Math.ceil(record.summaryButtonEl.getBoundingClientRect().height || 0);
        const expanded = record.root.dataset.expanded === '1';
        const timelineHeight = expanded
            ? Math.min(Math.ceil(record.timelineEl?.scrollHeight || 0), TIMELINE_MAX_HEIGHT)
            : 0;
        record.root.style.minHeight = `${Math.max(summaryHeight + timelineHeight, 0)}px`;
    }

    // Re-sync cards after SPA return or browser tab visibility restore.
    window.addEventListener('ouro:page-shown', (event) => {
        if (event?.detail?.page !== 'chat') return;
        for (const record of liveCardRecords.values()) {
            if (record?.root?.isConnected) syncLiveCardLayout(record);
        }
    });
    window.addEventListener('resize', () => {
        if (state.activePage !== 'chat') return;
        for (const record of liveCardRecords.values()) {
            if (!record?.root?.isConnected) continue;
            const expanded = record.root.dataset.expanded === '1';
            if (record.timelineEl) record.timelineEl.style.display = expanded ? 'flex' : 'none';
            syncLiveCardLayout(record);
        }
    });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) return;
        if (state.activePage !== 'chat') return;
        for (const record of liveCardRecords.values()) {
            if (record?.root?.isConnected && record._needsLayoutSync) syncLiveCardLayout(record);
        }
    });

    function buildTimelineItemHtml(item, record) {
        const expandable = isLiveLineExpandable(item);
        const expanded = expandable && record.expandedLineKeys.has(item.lineKey);
        const displayHeadline = expanded && item.fullHeadline ? item.fullHeadline : item.headline;
        const displayBody = expanded && item.fullBody ? item.fullBody : item.body;
        const isProgressLine = item.phase === 'working' || item.phase === 'thinking';
        const bodyId = `chat-live-line-body-${String(record.groupId || 'task').replace(/[^A-Za-z0-9_-]/g, '-')}-${String(item.lineKey || '').replace(/[^A-Za-z0-9_-]/g, '-')}`;
        const headContent = `
            <span class="chat-live-line-title">${isProgressLine ? renderMarkdown(displayHeadline) : escapeHtml(displayHeadline)}</span>
            <span class="chat-live-line-repeat" ${item.count > 1 ? '' : 'hidden'}>${item.count > 1 ? `${item.count}x` : ''}</span>
            ${item.ts ? `<span class="chat-live-line-time">${escapeHtml(item.ts)}</span>` : ''}
        `;
        const headHtml = expandable
            ? `
                <button
                    type="button"
                    class="chat-live-line-toggle"
                    data-live-line-toggle="${escapeHtmlAttr(item.lineKey)}"
                    aria-expanded="${expanded ? 'true' : 'false'}"
                    ${displayBody ? `aria-controls="${escapeHtmlAttr(bodyId)}"` : ''}
                >
                    <span class="chat-live-line-head">${headContent}</span>
                    <span class="chat-live-line-expand-label">${expanded ? 'Свернуть' : 'Развернуть'}</span>
                </button>
            `
            : `<div class="chat-live-line-head">${headContent}</div>`;
        return `
            <div
                class="chat-live-line ${item.phase || 'working'}${expandable ? ' expandable' : ''}"
                data-live-line-key="${escapeHtmlAttr(item.lineKey || '')}"
                data-expanded="${expanded ? '1' : '0'}"
            >
                ${headHtml}
                ${displayBody ? `<div class="chat-live-line-body" id="${escapeHtmlAttr(bodyId)}">${renderMarkdown(displayBody)}</div>` : ''}
            </div>
        `;
    }

    // Full rebuild for initial render and expand/collapse toggles.
    function renderLiveCardTimeline(record) {
        record.timelineEl.innerHTML = record.items.map((item) => buildTimelineItemHtml(item, record)).join('');
    }

    // Append without disturbing existing DOM nodes.
    function appendTimelineItem(item, record) {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const node = wrapper.firstElementChild;
        if (node) {
            record.timelineEl.appendChild(node);
            if (record.root.dataset.expanded === '1') {
                record.timelineEl.scrollTop = record.timelineEl.scrollHeight;
            }
        }
    }

    // Patch the last DOM node for dedup/count bumps.
    function patchLastTimelineItem(item, record) {
        const lastEl = record.timelineEl.lastElementChild;
        if (!lastEl) return renderLiveCardTimeline(record);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const newNode = wrapper.firstElementChild;
        if (newNode) record.timelineEl.replaceChild(newNode, lastEl);
    }

    // Patch a specific timeline node in place (evolving subagent dashboard rows).
    function patchTimelineItemAt(item, record) {
        const key = String(item.lineKey || '').replace(/[^A-Za-z0-9_-]/g, '');
        const el = key ? record.timelineEl.querySelector(`[data-live-line-key="${key}"]`) : null;
        if (!el) return renderLiveCardTimeline(record);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const newNode = wrapper.firstElementChild;
        if (newNode) record.timelineEl.replaceChild(newNode, el);
    }

    function scheduleHistorySync() {
        if (historySyncTimer) clearTimeout(historySyncTimer);
        historySyncTimer = setTimeout(() => {
            historySyncTimer = null;
            syncHistory({ includeUser: false }).catch(() => {});
        }, 700);
    }

    function applyLiveCardState(summary, groupId, ts, dedupeKey = '', { suppressDomInsert = false } = {}) {
        const nextGroupId = groupId || activeLiveGroupId || 'active';
        const record = getLiveCardRecord(nextGroupId);
        const nextPhase = summary.phase || '';
        if (record.finished && !isTerminalTaskPhase(nextPhase, summary.terminal)) {
            return;
        }

        if (!record.isSubagent) activeLiveGroupId = nextGroupId;
        ensureLiveCardVisible(record, { suppressDomInsert });
        record.updates += 1;
        const wasFinished = record.finished;
        const headline = summary.headline || 'Working...';
        const syntheticKey = summary.dedupeKey || dedupeKey || `${summary.phase || 'working'}|${headline}|${summary.body || ''}`;
        const isLegacyParentSubagentKey = syntheticKey.startsWith('parent-subagent:');
        const inPlaceByKey = isLegacyParentSubagentKey
            || syntheticKey.startsWith('subagent-lifecycle:')
            || syntheticKey.startsWith('subagent-progress:');
        if (!isLegacyParentSubagentKey) {
            record.finished = isTerminalTaskPhase(nextPhase, summary.terminal);
        }
        record.root.dataset.finished = record.finished ? '1' : '0';
        if (summary.human && headline) {
            record.lastHumanHeadline = headline;
        }

        const shouldPromote =
            Boolean(summary.promote)
            || !record.lastHumanHeadline
            || record.finished;
        const activeHeadline = shouldPromote
            ? headline
            : (record.lastHumanHeadline || headline);
        const activePhase = record.finished
            ? (summary.phase || 'done')
            : (shouldPromote ? (summary.phase || 'working') : (record.phaseEl.dataset.phase || 'working'));

        record.phaseEl.dataset.phase = activePhase;
        record.phaseEl.textContent = formatLiveCardPhaseLabel(activePhase);
        record.phaseEl.className = `chat-live-phase ${activePhase}`;
        record.titleEl.textContent = activeHeadline;

        const shouldRenderLine = summary.visible !== false && Boolean(headline || summary.body);
        // Legacy parent-subagent rows update in place if replayed from old
        // history. Child-card lifecycle/progress rows also evolve in place.
        let timelineUpdate = 'none';
        let patchIndex = -1;
        if (shouldRenderLine) {
            const lastIdx = record.items.length - 1;
            const existingIdx = inPlaceByKey
                ? record.items.findIndex((it) => it.dedupeKey === syntheticKey)
                : (lastIdx >= 0 && record.items[lastIdx].dedupeKey === syntheticKey ? lastIdx : -1);
            if (existingIdx !== -1 && inPlaceByKey) {
                const it = record.items[existingIdx];
                it.phase = summary.phase || it.phase;
                it.headline = headline || it.headline;
                it.fullHeadline = summary.fullHeadline || headline || it.fullHeadline;
                it.body = summary.body || '';
                it.fullBody = summary.fullBody || summary.body || it.fullBody || '';
                it.ts = ts || it.ts;
                patchIndex = existingIdx;
                timelineUpdate = 'patch-at';
            } else if (existingIdx !== -1) {
                const it = record.items[existingIdx];
                it.count += 1;
                it.ts = ts || it.ts;
                it.fullHeadline = summary.fullHeadline || it.fullHeadline || it.headline;
                it.fullBody = summary.fullBody || it.fullBody || it.body;
                timelineUpdate = 'patch-last';
            } else {
                const lineKey = `line-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                record.items.push({
                    phase: summary.phase || 'working',
                    headline: headline || 'Update',
                    fullHeadline: summary.fullHeadline || headline || 'Update',
                    body: summary.body || '',
                    fullBody: summary.fullBody || summary.body || '',
                    ts: ts || '',
                    count: 1,
                    dedupeKey: syntheticKey,
                    lineKey,
                });
                timelineUpdate = 'append';
            }
        }
        record.countEl.hidden = record.items.length < 2;
        record.countEl.textContent = `${record.items.length} ${record.items.length === 1 ? 'заметка' : (record.items.length < 5 ? 'заметки' : 'заметок')}`;
        record.metaEl.innerHTML = [
            nextGroupId === 'bg-consciousness' ? 'Фоновое мышление' : '',
            ...(Array.isArray(summary.meta) ? summary.meta : []),
            ts ? `Последнее ${ts}` : '',
        ].filter(Boolean).map((item) => `<span class="chat-live-meta-text">${escapeHtml(item)}</span>`).join('');
        // Incremental updates; full rebuilds stay limited to toggles.
        const lastItem = record.items[record.items.length - 1];
        if (timelineUpdate === 'append' && lastItem) {
            appendTimelineItem(lastItem, record);
        } else if (timelineUpdate === 'patch-last' && lastItem) {
            patchLastTimelineItem(lastItem, record);
        } else if (timelineUpdate === 'patch-at' && patchIndex !== -1) {
            patchTimelineItemAt(record.items[patchIndex], record);
        }
        ensureLiveCardVisible(record, { suppressDomInsert });
        syncLiveCardLayout(record);
        hideTypingIndicatorOnly();
        const justFinished = record.finished && !wasFinished;
        if (record.finished) {
            setLiveCardTypingVisible(record, false);
            markTaskComplete(nextGroupId, summary.phase || 'done');
            if (justFinished) {
                setLiveCardExpanded(record, false);
                scheduleHistorySync();
            }
            syncLiveCardToggle(record);
            setStatus(summary.phase === 'error' || summary.phase === 'timeout' ? 'error' : 'online', summary.phase === 'error' || summary.phase === 'timeout' ? 'Внимание' : 'Онлайн');
        } else {
            setLiveCardTypingVisible(record, true);
            setStatus('thinking', 'Работает...');
        }
    }

    function finishLiveCard(groupId = '', phase = '') {
        const record = groupId
            ? liveCardRecords.get(groupId)
            : (activeLiveGroupId ? liveCardRecords.get(activeLiveGroupId) : null);
        if (!record) return;
        const wasFinished = record.finished;
        record.finished = true;
        record.root.dataset.finished = '1';
        const activePhase = ['error', 'timeout'].includes(phase) ? phase : 'done';
        record.phaseEl.dataset.phase = activePhase;
        record.phaseEl.textContent = formatLiveCardPhaseLabel(activePhase);
        record.phaseEl.className = `chat-live-phase ${activePhase}`;
        setLiveCardTypingVisible(record, false);
        markTaskComplete(record.groupId, activePhase);
        if (!wasFinished) {
            setLiveCardExpanded(record, false);
            scheduleHistorySync();
        }
        syncLiveCardToggle(record);
        if (activeLiveGroupId === record.groupId) activeLiveGroupId = '';
        if (!hasActiveLiveCard()) {
            setStatus(activePhase === 'error' || activePhase === 'timeout' ? 'error' : 'online',
                      activePhase === 'error' || activePhase === 'timeout' ? 'Внимание' : 'Онлайн');
        }
    }

    function appendTaskSummaryToLiveCard(msg, { suppressDomInsert = false } = {}) {
        const taskId = msg?.task_id || activeLiveGroupId || '';
        if (!taskId) {
            finishLiveCard(taskId, 'done');
            return;
        }
        const taskState = getTaskUiState(taskId, false);
        if (!taskState) {
            finishLiveCard(taskId, 'done');
            return;
        }
        revealBufferedCardIfNeeded(taskState, { suppressDomInsert });
        if (!taskState.cardVisible) {
            markAssistantReply(taskId);
            return;
        }
        const record = liveCardRecords.get(taskId);
        const resultStatus = msg?.result_status ? String(msg.result_status) : '';
        const reasonCode = msg?.reason_code ? String(msg.reason_code) : '';
        const failedResult = ['failed', 'infra_failed'].includes(resultStatus);
        const doneHeadline = failedResult && reasonCode
            ? `Готово: ${reasonCode}`
            : ((record && record.lastHumanHeadline) || 'Готово');
        applyLiveCardState(
            {
                phase: failedResult ? 'error' : 'done',
                headline: doneHeadline,
                visible: false,
                human: false,
                promote: true,
                terminal: true,
            },
            taskId,
            normalizeLogTs(msg.ts || new Date().toISOString()),
            `task_done|${taskId}`,
            { suppressDomInsert },
        );
        finishLiveCard(taskId, failedResult ? 'error' : 'done');
        scheduleTaskUiCleanup(taskState);
    }

    // child task_id -> { parentId, role }, learned from subagent lifecycle pings.
    // Child cards are mounted under the parent card, but their phase/terminal
    // state is independent so a finished child cannot mark the parent done.
    const subagentChildParents = new Map();
    // Children whose card has reached a terminal phase — late non-lifecycle
    // progress for these must NOT revive it back to "working".
    const subagentTerminalChildren = new Set();

    const SUBAGENT_EVENT_PHASE = {
        scheduled: 'start', running: 'working', completed: 'done',
        failed: 'error', rejected: 'warn', cancelled: 'warn', interrupted: 'warn',
    };
    const SUBAGENT_EVENT_LABEL = {
        scheduled: 'scheduled', running: 'running', completed: 'done',
        failed: 'failed', rejected: 'rejected', cancelled: 'cancelled', interrupted: 'interrupted',
    };

    function updateLiveCardFromProgressMessage(msg) {
        const taskId = msg?.task_id || activeLiveGroupId || '';
        if (!taskId) return;
        // Subagent lifecycle pings render as child cards linked to the parent;
        // they must not update the parent card's terminal state.
        const lifecycleParent = String(msg?.parent_task_id || '').trim();
        if (msg?.subagent_event && lifecycleParent && lifecycleParent !== taskId) {
            updateSubagentCardFromEvent(msg, msg.ts || new Date().toISOString());
            return;
        }
        // A known subagent child's own (non-lifecycle) progress stays on the child
        // card so parallel work remains visible without expanding the parent.
        if (subagentChildParents.has(taskId)) {
            routeSubagentProgressToCard(taskId, msg);
            return;
        }
        // Progress messages are visible status; do not force-open completed replay.
        const taskState = getTaskUiState(taskId, true);
        if (taskState && !taskState.completed) taskState.forceCard = true;
        const summary = summarizeChatLiveEvent({
            type: 'send_message',
            is_progress: true,
            content: msg?.content || msg?.text || '',
            text: msg?.content || msg?.text || '',
            task_id: taskId,
            subagent_event: msg?.subagent_event || '',
            subagent_task_id: msg?.subagent_task_id || '',
            root_task_id: msg?.root_task_id || '',
            parent_task_id: msg?.parent_task_id || '',
            delegation_role: msg?.delegation_role || '',
            subagent_role: msg?.subagent_role || '',
            status: msg?.status || '',
            cost_usd: msg?.cost_usd || 0,
            result: msg?.result || '',
            trace_summary: msg?.trace_summary || '',
            error: msg?.error || '',
            artifact_status: msg?.artifact_status || '',
            lifecycle: msg?.lifecycle || null,
        });
        if (!summary) return;
        queueTaskLiveUpdate(summary, taskId, normalizeLogTs(msg.ts || new Date().toISOString()), summary.dedupeKey || '');
    }

    function updateSubagentCardFromEvent(evt, tsValue) {
        if (!evt || String(evt.delegation_role || '').toLowerCase() !== 'subagent') return false;
        const parentId = String(evt.parent_task_id || '').trim();
        const childId = String(evt.subagent_task_id || evt.task_id || '').trim();
        if (!parentId || !childId || parentId === childId) return false;
        const event = String(evt.subagent_event || 'update').toLowerCase();
        const role = String(evt.subagent_role || '').trim();
        subagentChildParents.set(childId, { parentId, role });
        // NOTE: 'interrupted' is intentionally excluded — it is retryable
        // (written before requeue), so the child resumes and its later progress
        // must still flow to its card. Only true terminals lock it.
        if (['completed', 'failed', 'cancelled', 'rejected'].includes(event)) {
            subagentTerminalChildren.add(childId);  // lock the child card terminal
        }
        const phase = SUBAGENT_EVENT_PHASE[event] || 'working';
        const label = SUBAGENT_EVENT_LABEL[event] || event;
        const shortChild = childId.slice(0, 8);
        const headline = role
            ? `Subagent ${shortChild} · ${role} — ${label}`
            : `Subagent ${shortChild} — ${label}`;
        // Surface the child's handoff (result/trace/error) as expandable detail
        // on the child card.
        const detailParts = [];
        if (evt.result) detailParts.push(`[RESULT]\n${String(evt.result)}`);
        if (evt.trace_summary) detailParts.push(`[TRACE]\n${String(evt.trace_summary)}`);
        if (evt.error) detailParts.push(`[ERROR]\n${String(evt.error)}`);
        const cost = Number(evt.cost_usd || 0);
        const metaBits = [`child=${shortChild}`];
        if (role) metaBits.push(`role=${role}`);
        if (cost > 0) metaBits.push(`cost=$${cost.toFixed(2)}`);
        forceTaskCard(parentId);
        const childState = getTaskUiState(childId, true);
        if (childState && !childState.completed) childState.forceCard = true;
        getSubagentCardRecord(childId, parentId, role);
        queueTaskLiveUpdate({
            phase,
            headline,
            body: '',
            fullBody: detailParts.join('\n\n'),
            visible: true,
            promote: true,
            meta: metaBits,
            dedupeKey: `subagent-lifecycle:${childId}`,
            terminal: ['completed', 'failed', 'cancelled', 'rejected'].includes(event),
        }, childId, normalizeLogTs(tsValue || new Date().toISOString()), `subagent-lifecycle:${childId}`);
        return true;
    }

    // A known child's own (non-lifecycle) progress updates the linked child card.
    function routeSubagentProgressToCard(childId, msg) {
        const info = subagentChildParents.get(childId);
        if (!info) return;
        if (subagentTerminalChildren.has(childId)) return;  // never revive a finished child
        const { parentId, role } = info;
        const shortChild = String(childId).slice(0, 8);
        const line = String(msg?.content || msg?.text || '').trim().split('\n').filter(Boolean).pop() || '';
        const headline = role
            ? `Subagent ${shortChild} · ${role} — running`
            : `Subagent ${shortChild} — running`;
        forceTaskCard(parentId);
        const childState = getTaskUiState(childId, true);
        if (childState && !childState.completed) childState.forceCard = true;
        getSubagentCardRecord(childId, parentId, role);
        const meta = [`child=${shortChild}`];
        if (role) meta.push(`role=${role}`);
        queueTaskLiveUpdate({
            phase: 'working',
            headline,
            body: line.slice(0, 200),
            visible: true,
            promote: true,
            meta,
            dedupeKey: `subagent-progress:${childId}`,
        }, childId, normalizeLogTs(msg?.ts || new Date().toISOString()), `subagent-progress:${childId}`);
    }

    function routeSubagentFinalMessageToCard(taskId, msg) {
        const childId = String(taskId || '').trim();
        const info = subagentChildParents.get(childId);
        if (!childId || !info) return false;
        const { parentId, role } = info;
        const shortChild = childId.slice(0, 8);
        const text = String(msg?.content || msg?.text || '').trim();
        forceTaskCard(parentId);
        getSubagentCardRecord(childId, parentId, role);
        const meta = [`child=${shortChild}`];
        if (role) meta.push(`role=${role}`);
        queueTaskLiveUpdate({
            phase: 'done',
            headline: role
                ? `Subagent ${shortChild} · ${role} — result`
                : `Subagent ${shortChild} — result`,
            body: text.slice(0, 200),
            fullBody: text,
            visible: true,
            promote: true,
            meta,
            dedupeKey: `subagent-result:${childId}`,
            terminal: true,
        }, childId, normalizeLogTs(msg?.ts || new Date().toISOString()), `subagent-result:${childId}`);
        return true;
    }

    // Resolve a child's card from the child's terminal task_done
    // (which arrives on the log channel without subagent metadata).
    function routeSubagentTerminalToCard(childId, evt) {
        const info = subagentChildParents.get(childId);
        if (!info) return false;
        const resultStatus = String(evt.result_status || '').toLowerCase();
        const status = String(evt.status || '').toLowerCase();
        const failed = ['failed', 'infra_failed'].includes(resultStatus) || status === 'failed';
        const cancelled = status === 'cancelled' || status === 'cancel_requested' || resultStatus === 'cancelled';
        const rejected = status === 'rejected_duplicate' || resultStatus === 'rejected_duplicate';
        const event = failed ? 'failed' : cancelled ? 'cancelled' : rejected ? 'rejected' : 'completed';
        updateSubagentCardFromEvent({
            delegation_role: 'subagent',
            parent_task_id: info.parentId,
            subagent_task_id: childId,
            subagent_role: info.role,
            subagent_event: event,
            result: evt.result || '',
            error: evt.error || '',
        }, evt.ts || evt.timestamp || new Date().toISOString());
        return true;
    }

    function updateLiveCardFromLogEvent(evt) {
        if (!evt || !isGroupedTaskEvent(evt)) return;
        const taskId = getLogTaskGroupId(evt) || activeLiveGroupId || '';
        if (!taskId) return;
        const eventType = evt.type || evt.event || '';
        // A known subagent child's log events update its linked child card.
        if (subagentChildParents.has(taskId)) {
            if (eventType === 'task_done') {
                routeSubagentTerminalToCard(taskId, evt);
                return;
            }
            if (subagentTerminalChildren.has(taskId)) return;
            if (eventType === 'tool_call_started') {
                markTaskToolCall(taskId, 1);
            } else if ((eventType === 'task_metrics_event' || eventType === 'task_eval') && Number.isFinite(Number(evt.tool_calls))) {
                markTaskToolCall(taskId, Number(evt.tool_calls), true);
            } else if (
                eventType === 'tool_call_timeout'
                || eventType === 'tool_timeout'
                || eventType === 'llm_round_error'
                || eventType === 'llm_api_error'
                || (eventType === 'tool_call_finished' && evt.is_error)
            ) {
                forceTaskCard(taskId);
            }
            const summary = summarizeChatLiveEvent(evt);
            if (!summary) return;
            const info = subagentChildParents.get(taskId);
            if (info) getSubagentCardRecord(taskId, info.parentId, info.role);
            queueTaskLiveUpdate(summary, taskId, normalizeLogTs(evt.ts || evt.timestamp), summary.dedupeKey || '');
            return;
        }
        if (eventType === 'tool_call_started') {
            markTaskToolCall(taskId, 1);
        } else if ((eventType === 'task_metrics_event' || eventType === 'task_eval') && Number.isFinite(Number(evt.tool_calls))) {
            markTaskToolCall(taskId, Number(evt.tool_calls), true);
        } else if (
            eventType === 'tool_call_timeout'
            || eventType === 'tool_timeout'
            || eventType === 'llm_round_error'
            || eventType === 'llm_api_error'
            || (eventType === 'tool_call_finished' && evt.is_error)
        ) {
            forceTaskCard(taskId);
        }
        const summary = summarizeChatLiveEvent(evt);
        if (!summary) return;
        queueTaskLiveUpdate(summary, taskId, normalizeLogTs(evt.ts || evt.timestamp), summary.dedupeKey || '');
        updateSubagentCardFromEvent(evt, evt.ts || evt.timestamp || new Date().toISOString());
        if (eventType === 'task_done') {
            const taskState = getTaskUiState(taskId, false);
            revealBufferedCardIfNeeded(taskState);
        }
    }

    function addMessage(text, role, markdown = false, timestamp = null, isProgress = false, opts = {}) {
        const pending = !!opts.pending;
        const ephemeral = !!opts.ephemeral;
        const clientMessageId = opts.clientMessageId || '';
        const senderLabel = opts.senderLabel || '';
        const senderSessionId = opts.senderSessionId || '';
        const source = opts.source || '';
        const systemType = opts.systemType || '';
        const taskId = opts.taskId || '';
        const ts = timestamp || new Date().toISOString();
        const messageKey = buildMessageKey(role, text, ts, {
            clientMessageId,
            systemType,
            isProgress,
            source,
            senderLabel,
            senderSessionId,
            taskId,
        });
        if (messageKey && seenMessageKeys.has(messageKey)) return null;

        if (!isProgress && !ephemeral) {
            persistedHistory.push({
                text,
                role,
                ts,
                markdown: !!markdown,
                systemType,
                source,
                senderLabel,
                senderSessionId,
                clientMessageId,
                taskId,
            });
            persistVisibleHistory();
        }

        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}` + (isProgress ? ' progress' : '');
        if (pending) bubble.classList.add('pending');
        if (ephemeral) bubble.dataset.ephemeral = '1';
        if (clientMessageId) bubble.dataset.clientMessageId = clientMessageId;
        if (systemType) bubble.dataset.systemType = systemType;
        if (senderSessionId) bubble.dataset.senderSessionId = senderSessionId;
        if (taskId) bubble.dataset.taskId = taskId;

        const sender = getSenderLabel(role, isProgress, systemType, { source, senderLabel, senderSessionId });
        const rendered = role === 'user'
            ? escapeHtml(text)
            : (role === 'system' && systemType === 'skill_review'
                ? renderSkillReviewDisclosure(text)
                : renderMarkdown(text));
        const timeFmt = formatMsgTime(ts);
        const timeHtml = timeFmt ? `<div class="msg-time" title="${escapeHtmlAttr(timeFmt.full)}">${escapeHtml(timeFmt.short)}</div>` : '';
        const pendingHtml = pending ? `<div class="msg-pending">В очереди до переподключения</div>` : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            <div class="message">${rendered}</div>
            ${pendingHtml}
            ${timeHtml}
        `;
        const skillReviewToggle = bubble.querySelector('[data-skill-review-toggle]');
        if (skillReviewToggle) {
            skillReviewToggle.addEventListener('click', () => {
                const disclosure = bubble.querySelector('[data-skill-review-disclosure]');
                const full = bubble.querySelector('[data-skill-review-full]');
                const label = bubble.querySelector('.skill-review-toggle-label');
                const expanded = disclosure?.dataset.expanded === '1';
                if (!disclosure || !full) return;
                disclosure.dataset.expanded = expanded ? '0' : '1';
                full.hidden = expanded;
                skillReviewToggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
                if (label) label.textContent = expanded ? 'Показать ревью' : 'Скрыть ревью';
                requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: true }));
            });
        }
        insertMessageNode(bubble, { forceStick: !!opts.forceStick });
        rememberMessageKey(messageKey);
        if (pending && clientMessageId) pendingUserBubbles.set(clientMessageId, bubble);
        return bubble;
    }

    function markPendingDelivered(clientMessageId) {
        const bubble = pendingUserBubbles.get(clientMessageId || '');
        if (!bubble) return;
        bubble.classList.remove('pending');
        bubble.querySelector('.msg-pending')?.remove();
        pendingUserBubbles.delete(clientMessageId);
    }

    function ensureWelcomeMessage() {
        if (welcomeShown) return;
        const hasRealBubbles = Array.from(messagesDiv.querySelectorAll('.chat-bubble')).some(
            bubble => !bubble.classList.contains('typing-bubble')
        );
        if (hasRealBubbles) return;
        welcomeShown = true;
        addMessage('Ouroboros пробудился', 'assistant', false, null, false, { ephemeral: true });
    }

    async function syncHistory({ includeUser = false, fromReconnect = false } = {}) {
        if (historySyncPromise) {
            // Preserve reconnect intent so retiredTaskIds is cleared after this sync.
            if (fromReconnect) pendingReconnectSync = true;
            return historySyncPromise;
        }
        historySyncPromise = (async () => {
            try {
                const resp = await apiFetch('/api/chat/history?limit=1000', { cache: 'no-store' });
                if (!resp.ok) return false;
                const data = await resp.json();
                const messages = Array.isArray(data.messages) ? data.messages : [];

                // First load/reconnect trusts server history and rebuilds retired cards.
                // Routine post-completion syncs keep retiredTaskIds to avoid duplicates.
                if (!historyLoaded || fromReconnect) retiredTaskIds.clear();
                if (!historyLoaded || fromReconnect) {
                    for (const record of liveCardRecords.values()) record.root?.remove();
                    liveCardRecords.clear();
                    taskUiStates.clear();
                    activeLiveGroupId = '';
                }

                // Two passes ensure cards exist before finishLiveCard() marks them done.

                // Pass 1 builds timelines with DOM insertion suppressed.
                _syncPass1Active = true;
                try { for (const msg of messages) {
                    const taskId = msg.task_id || '';
                    if (!taskId) continue;
                    if (retiredTaskIds.has(taskId)) continue;
                    if (msg.is_progress) {
                        updateLiveCardFromProgressMessage(msg);
                        continue;
                    }
                    if (msg.system_type === 'task_summary') {
                        // Historical cards only for non-trivial tasks.
                        const hadToolCalls = (msg.tool_calls || 0) > 0;
                        const hadMultipleRounds = (msg.rounds || 0) > 1;
                        const failedResult = ['failed', 'infra_failed'].includes(String(msg.result_status || ''));
                        if (hadToolCalls || hadMultipleRounds || failedResult) {
                            const taskState = getTaskUiState(taskId, true);
                            if (taskState) taskState.forceCard = true;
                        }
                        // Pass 2 inserts this in the right transcript position.
                        appendTaskSummaryToLiveCard(msg, { suppressDomInsert: true });
                    }
                } } finally { _syncPass1Active = false; }

                // Pass 2 inserts cards at the first visible task message, then finishes them.
                const insertedCardTaskIds = new Set();
                function insertCardIfNeeded(taskId) {
                    if (!taskId || insertedCardTaskIds.has(taskId)) return;
                    insertedCardTaskIds.add(taskId);
                    const rec = liveCardRecords.get(taskId);
                    if (rec && rec.root && !rec.root.isConnected) {
                        if (rec.isSubagent) ensureLiveCardVisible(rec);
                        else insertMessageNode(rec.root);
                    }
                }
                for (const msg of messages) {
                    const taskId = msg.task_id || '';
                    if (!includeUser && msg.role === 'user') continue;
                    if (msg.is_progress) {
                        // Progress-only/failed tasks still anchor at their first event.
                        insertCardIfNeeded(taskId);
                        continue;
                    }
                    if (msg.system_type === 'task_summary') continue;
                    if (taskId && (msg.role === 'assistant' || msg.role === 'system')) {
                        if (subagentChildParents.has(taskId)) {
                            insertCardIfNeeded(taskId);
                            routeSubagentFinalMessageToCard(taskId, msg);
                            const taskState = getTaskUiState(taskId, false);
                            const record = liveCardRecords.get(taskId);
                            const preservedPhase = taskState?.completedPhase || record?.phaseEl?.dataset?.phase || 'done';
                            finishLiveCard(taskId, preservedPhase);
                            continue;
                        }
                        insertCardIfNeeded(taskId);
                        const taskState = getTaskUiState(taskId, false);
                        const record = liveCardRecords.get(taskId);
                        const preservedPhase = taskState?.completedPhase || record?.phaseEl?.dataset?.phase || 'done';
                        finishLiveCard(taskId, preservedPhase);
                    }
                    addMessage(msg.text, msg.role, !!msg.markdown, msg.ts || null, false, {
                        systemType: msg.system_type || '',
                        source: msg.source || '',
                        senderLabel: msg.sender_label || '',
                        senderSessionId: msg.sender_session_id || '',
                        clientMessageId: msg.client_message_id || '',
                        taskId,
                    });
                }
                // Resolve cards whose task is already terminal on the server
                // (crash storm / hard timeout / cancellation write a terminal
                // status but no task_summary). Without this their progress-only
                // cards re-inflate as "Working" forever on reload/reconnect.
                const terminalTaskStatus = new Map();
                for (const msg of messages) {
                    const tid = msg.task_id || '';
                    if (tid && msg.task_terminal_status) {
                        terminalTaskStatus.set(tid, String(msg.task_terminal_status));
                    }
                }
                for (const [tid, status] of terminalTaskStatus) {
                    // Subagent terminal status resolves the child card, not the
                    // parent. Otherwise reload can revive a crashed/cancelled child.
                    if (subagentChildParents.has(tid)) {
                        routeSubagentTerminalToCard(tid, { status, result_status: status });
                        continue;
                    }
                    const rec = liveCardRecords.get(tid);
                    if (rec && !rec.finished) {
                        insertCardIfNeeded(tid);
                        finishLiveCard(tid, status === 'failed' ? 'error' : 'done');
                    }
                }

                // Append disconnected visible cards after mid-task reload; skip trivial placeholders.
                for (const [tid, rec] of liveCardRecords) {
                    if (rec && rec.root && !rec.root.isConnected && !retiredTaskIds.has(tid)) {
                        const ts = taskUiStates.get(tid);
                        if (ts && !ts.cardVisible && ts.completed) continue;
                        if (rec.isSubagent) ensureLiveCardVisible(rec);
                        else insertMessageNode(rec.root);
                    }
                }

                // After first load, unfinished visible cards still show typing.
                if (!historyLoaded) {
                    const hasOngoingTask = Array.from(liveCardRecords.values()).some(
                        (record) => record?.root?.isConnected && !record.finished
                    );
                    if (hasOngoingTask) showTyping();
                }

                // One-shot server recall seed includes other clients without resetting
                // ArrowUp during reconnect. Merge [server..., local...], newest wins.
                if (!inputHistorySeededFromServer) {
                    const serverTexts = [];
                    for (const msg of messages) {
                        if (msg.role !== 'user') continue;
                        let text = (msg.text || '').trim();
                        if (text.startsWith(PLAN_PREFIX)) text = text.slice(PLAN_PREFIX.length).trimStart();
                        if (text) serverTexts.push(text);
                    }
                    const combined = [...serverTexts, ...inputHistory];
                    const deduped = [];
                    const seen = new Set();
                    for (let i = combined.length - 1; i >= 0; i--) {
                        if (!seen.has(combined[i])) {
                            deduped.unshift(combined[i]);
                            seen.add(combined[i]);
                        }
                    }
                    inputHistory.length = 0;
                    inputHistory.push(...deduped.slice(-50));
                    saveInputHistory(inputHistory);
                    inputHistoryIndex = inputHistory.length;
                    inputHistorySeededFromServer = true;
                }

                const wasFirstLoad = !historyLoaded;
                historyLoaded = true;
                // First load jumps to latest; reconnect preserves older-message reading.
                if (wasFirstLoad || isNearBottom()) {
                    updateMessagesPadding({ preserveStickiness: false });
                    scrollToBottomAfterLayout();
                }
                return messages.length > 0;
            } catch (err) {
                const socketState = ws?.ws?.readyState;
                const expectedDisconnect = socketState !== WebSocket.OPEN;
                if (expectedDisconnect && err instanceof TypeError) {
                    return false;
                }
                console.error('Failed to load chat history:', err);
                return false;
            } finally {
                historySyncPromise = null;
                // Replay queued reconnect sync with fresh server state.
                if (pendingReconnectSync) {
                    pendingReconnectSync = false;
                    syncHistory({ includeUser: false, fromReconnect: true }).catch(() => {});
                }
            }
        })();
        return historySyncPromise;
    }

    (async () => {
        if (await syncHistory({ includeUser: true })) return;
        try {
            const saved = JSON.parse(sessionStorage.getItem(CHAT_STORAGE_KEY) || '[]');
            for (const msg of saved) {
                addMessage(msg.text, msg.role, !!msg.markdown, msg.ts || null, false, {
                    systemType: msg.systemType || '',
                    source: msg.source || '',
                    senderLabel: msg.senderLabel || '',
                    senderSessionId: msg.senderSessionId || '',
                    clientMessageId: msg.clientMessageId || '',
                    taskId: msg.taskId || '',
                });
            }
        } catch {}
        historyLoaded = true;
        ensureWelcomeMessage();
    })();

    function rememberInput(text) {
        if (!text) return;
        if (inputHistory[inputHistory.length - 1] !== text) inputHistory.push(text);
        saveInputHistory(inputHistory);
        inputHistoryIndex = inputHistory.length;
        inputDraft = '';
    }

    function resizeChatInput({ preserveStickiness = false } = {}) {
        input.style.removeProperty('height');
        updateMessagesPadding({ preserveStickiness });
    }

    function restoreInputHistory(step) {
        if (!inputHistory.length) return;
        if (step < 0) {
            if (input.selectionStart !== 0 || input.selectionEnd !== 0) return;
            if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
            inputHistoryIndex = Math.max(0, inputHistoryIndex - 1);
            input.value = inputHistory[inputHistoryIndex] || '';
        } else {
            if (input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length) return;
            inputHistoryIndex = Math.min(inputHistory.length, inputHistoryIndex + 1);
            input.value = inputHistoryIndex === inputHistory.length ? inputDraft : (inputHistory[inputHistoryIndex] || '');
        }
        resizeChatInput({ preserveStickiness: false });
        const cursor = input.value.length;
        input.setSelectionRange(cursor, cursor);
    }

    async function sendMessage(planMode = false) {
        if (sendBtn.disabled) return;  // guard against Enter re-entry during async upload
        let text = input.value.trim();
        const hasAttachments = pendingAttachments.length > 0;
        let uploadedAttachments = [];
        if (!text && !pendingAttachments.length) return;
        if (pendingAttachments.length) {
            // Upload immediately before send; offline queueing would orphan files.
            if (ws.ws?.readyState !== WebSocket.OPEN) {
                showToast('Cannot attach file while offline. Reconnect and try again.', 'error');
                return;
            }
            const staged = [...pendingAttachments];
            const uploaded = [];
            setAttachmentUploadState(true);
            setSendBusy(true, staged.length > 1 ? 'Загрузка файлов' : 'Загрузка');
            try {
                for (const stagedItem of staged) {
                    if (ws.ws?.readyState !== WebSocket.OPEN) throw new Error('Connection closed during upload. Reconnect and try again.');
                    const formData = new FormData();
                    formData.append('file', stagedItem.file);
                    const resp = await apiFetch('/api/chat/upload', { method: 'POST', body: formData });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || !data.ok) {
                        throw new Error(data.error || resp.statusText);
                    }
                    uploaded.push({
                        filename: data.filename || '',
                        path: data.path || '',
                        display_name: data.display_name || stagedItem.display_name,
                    });
                }
                if (ws.ws?.readyState !== WebSocket.OPEN) throw new Error('Connection closed after upload. Reconnect and try again.');
                uploadedAttachments = uploaded;
                const attachmentLines = uploaded
                    .map((item) => `[Attached file: ${item.display_name} saved to ${item.path}]`)
                    .join('\n');
                text += (text ? '\n\n' : '') + attachmentLines;
            } catch (e) {
                await cleanupUploadedAttachments(uploaded);
                showToast('Upload error: ' + e.message, 'error');
                return;  // pending attachments and preview remain so the user can retry
            } finally {
                setAttachmentUploadState(false);
                setSendBusy(false);
            }
        }
        if (!text) return;
        // Plan prefix is wire-only; slash commands stay literal.
        const wireText = (planMode && !text.startsWith('/')) ? PLAN_PREFIX + text : text;
        const result = ws.send({
            type: 'chat',
            content: wireText,
            sender_session_id: chatSessionId,
        }, hasAttachments ? { queue: false } : undefined);
        if (hasAttachments && result?.status !== 'sent') {
            await cleanupUploadedAttachments(uploadedAttachments);
            showToast('Connection lost before send. Reconnect and try again.', 'error');
            return;
        }
        if (hasAttachments) {
            pendingAttachments = [];
            updateAttachmentPreview();
        }
        rememberInput(text);
        input.value = '';
        addMessage(text, 'user', false, null, false, {
            pending: result?.status === 'queued',
            source: 'web',
            senderSessionId: chatSessionId,
            clientMessageId: result?.clientMessageId || '',
            forceStick: true,
        });
        resizeChatInput({ preserveStickiness: false });
        scrollToBottomAfterLayout();
    }

    // Send mode lives on DOM so CSS and click/Enter share one source.
    const sendGroup = document.querySelector('.chat-send-group');

    function setSendMode(mode) {
        sendGroup.dataset.sendMode = mode;
        sendBtn.textContent = mode === 'plan' ? 'Планировать' : 'Отправить';
        sendBtn.title = mode === 'plan' ? 'Отправить с префиксом планирования' : 'Отправить сообщение';
        // Mark the active item in the dropdown for visual feedback.
        dropdownSend.dataset.modeActive = mode === 'send' ? 'true' : 'false';
        dropdownPlan.dataset.modeActive = mode === 'plan' ? 'true' : 'false';
    }

    function setSendBusy(busy, label = '') {
        sendGroup.dataset.busy = busy ? '1' : '0';
        sendBtn.disabled = busy;
        chevronBtn.disabled = busy;
        if (busy) {
            sendBtn.textContent = label || 'Отправка';
            sendBtn.title = label || 'Отправка';
        } else {
            setSendMode(sendGroup.dataset.sendMode || 'send');
        }
    }

    setSendMode('send');

    function openSendDropdown() {
        sendDropdown.classList.add('open');
        chevronBtn.classList.add('active');
    }
    function closeSendDropdown() {
        sendDropdown.classList.remove('open');
        chevronBtn.classList.remove('active');
    }
    chevronBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (sendDropdown.classList.contains('open')) {
            closeSendDropdown();
        } else {
            openSendDropdown();
        }
    });
    dropdownSend.addEventListener('click', () => {
        setSendMode('send');
        closeSendDropdown();
    });
    dropdownPlan.addEventListener('click', () => {
        setSendMode('plan');
        closeSendDropdown();
    });
    document.addEventListener('click', (e) => {
        if (!sendDropdown.contains(e.target) && e.target !== chevronBtn) {
            closeSendDropdown();
        }
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeSendDropdown();
    });

    // Arrow wrappers avoid MouseEvent leaking into sendMessage(planMode).
    sendBtn.addEventListener('click', () => sendMessage(sendGroup.dataset.sendMode === 'plan'));
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage(sendGroup.dataset.sendMode === 'plan');
            return;
        }
        if (e.key === 'ArrowUp' && !e.shiftKey) {
            restoreInputHistory(-1);
        } else if (e.key === 'ArrowDown' && !e.shiftKey) {
            restoreInputHistory(1);
        }
    });
    // Dynamic CSS reserve keeps the absolute composer from covering messages.
    function scrollToBottom() {
        shouldAutoStickToBottom = true;
        setProgrammaticScrollTop(messagesDiv.scrollHeight);
    }

    function scrollToBottomAfterLayout() {
        requestAnimationFrame(() => {
            scrollToBottom();
            requestAnimationFrame(scrollToBottom);
        });
    }

    function updateMessagesPadding(options = {}) {
        const preserveStickiness = options.preserveStickiness !== false;
        const shouldStick = preserveStickiness && isNearBottom();
        if (inputArea && messagesDiv) {
            const reserve = Math.max(92, Math.ceil(inputArea.offsetHeight || 0) + 16);
            messagesDiv.style.setProperty('--chat-input-reserve', `${reserve}px`);
        }
        if (shouldStick) scrollToBottomAfterLayout();
    }

    function installChatResizeObservers() {
        if (typeof ResizeObserver !== 'function') return;
        let queued = false;
        const schedule = () => {
            if (queued) return;
            queued = true;
            requestAnimationFrame(() => {
                queued = false;
                updateMessagesPadding({ preserveStickiness: true });
            });
        };
        const observer = new ResizeObserver(schedule);
        if (inputArea) observer.observe(inputArea);
        if (messagesDiv) observer.observe(messagesDiv);
    }

    installChatResizeObservers();
    messagesDiv.addEventListener('scroll', setStickyByUserScroll, { passive: true });

    input.addEventListener('input', () => {
        if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
        resizeChatInput({ preserveStickiness: false });
    });

    function closeActionMenu() {
        actionMenu?.classList.remove('open');
        actionTrigger?.setAttribute('aria-expanded', 'false');
    }

    actionTrigger?.addEventListener('click', (event) => {
        event.stopPropagation();
        const isOpen = actionMenu?.classList.contains('open');
        if (isOpen) closeActionMenu();
        else {
            actionMenu?.classList.add('open');
            actionTrigger.setAttribute('aria-expanded', 'true');
        }
    });

    headerActions?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-chat-command]');
        if (!button) return;
        const command = button.dataset.chatCommand;
        if (command === 'evolve') {
            const next = !button.classList.contains('on');
            button.classList.toggle('on', next);
            ws.send({ type: 'command', cmd: `/evolve ${next ? 'start' : 'stop'}` });
            closeActionMenu();
            return;
        }
        if (command === 'bg') {
            const next = !button.classList.contains('on');
            button.classList.toggle('on', next);
            ws.send({ type: 'command', cmd: `/bg ${next ? 'start' : 'stop'}` });
            closeActionMenu();
            return;
        }
        if (command === 'review') {
            ws.send({ type: 'command', cmd: '/review' });
            closeActionMenu();
            return;
        }
        if (command === 'restart') {
            ws.send({ type: 'command', cmd: '/restart' });
            closeActionMenu();
            return;
        }
        if (command === 'panic' && confirm('Немедленно остановить всех воркеров?')) {
            ws.send({ type: 'command', cmd: '/panic' });
        }
        closeActionMenu();
    });

    document.addEventListener('click', (event) => {
        if (actionMenu && !actionMenu.contains(event.target)) closeActionMenu();
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeActionMenu();
    });

    budgetPill?.addEventListener('click', () => {
        if (typeof openDashboardTab === 'function') openDashboardTab('costs');
        else if (typeof openSettingsTab === 'function') openSettingsTab('costs');
    });
    budgetPill?.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        budgetPill.click();
    });

    refreshHeaderControlState(true);
    setInterval(refreshHeaderControlState, 3000);

    const typingEl = document.createElement('div');
    typingEl.id = 'typing-indicator';
    typingEl.className = 'chat-bubble assistant typing-bubble';
    typingEl.style.display = 'none';
    typingEl.innerHTML = `<div class="typing-dots"><span></span><span></span><span></span></div>`;
    messagesDiv.appendChild(typingEl);

    function hasActiveLiveCard() {
        return Array.from(liveCardRecords.values()).some((record) => record?.root?.isConnected && !record.finished);
    }

    function showTyping() {
        if (!hasActiveLiveCard()) {
            typingEl.style.display = '';
            if (shouldAutoStickToBottom && isNearBottom()) scrollToBottom();
        }
        setStatus('thinking', 'Думает...');
    }

    function hideTypingIndicatorOnly() {
        typingEl.style.display = 'none';
    }

    function hideTyping() {
        hideTypingIndicatorOnly();
        if (statusBadge && ['Думает...', 'Работает...'].includes(statusBadge.textContent)) {
            setStatus('online', 'Онлайн');
        }
    }

    function incrementUnreadIfNeeded() {
        if (state.activePage === 'chat') return;
        state.unreadCount++;
        updateUnreadBadge();
    }

    ws.on('typing', () => {
        showTyping();
    });

    ws.on('chat', (msg) => {
        if (msg.role === 'user') {
            const clientMessageId = msg.client_message_id || '';
            const senderSessionId = msg.sender_session_id || '';
            if (senderSessionId === chatSessionId && clientMessageId) {
                markPendingDelivered(clientMessageId);
                return;
            }
            addMessage(msg.content, 'user', false, msg.ts || null, false, {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId,
                clientMessageId,
                taskId: msg.task_id || '',
            });
            incrementUnreadIfNeeded();
            return;
        }

        if (msg.role === 'assistant' || msg.role === 'system') {
            hideTyping();
            const explicitTaskId = msg.task_id || '';
            if (msg.is_progress) {
                updateLiveCardFromProgressMessage(msg);
                return;
            }
            if (msg.system_type === 'task_summary') {
                appendTaskSummaryToLiveCard(msg);
                markAssistantReply(explicitTaskId);
                incrementUnreadIfNeeded();
                return;
            }
            if (explicitTaskId && subagentChildParents.has(explicitTaskId)) {
                routeSubagentFinalMessageToCard(explicitTaskId, msg);
                markAssistantReply(explicitTaskId);
                incrementUnreadIfNeeded();
                return;
            }
            if (explicitTaskId) finishLiveCard(explicitTaskId);
            markAssistantReply(explicitTaskId);
            addMessage(msg.content, msg.role, msg.markdown, msg.ts || null, false, {
                systemType: msg.system_type || '',
                source: msg.source || '',
                taskId: explicitTaskId,
            });
            incrementUnreadIfNeeded();
        }
    });

    ws.on('log', (msg) => {
        if (!msg?.data) return;
        updateLiveCardFromLogEvent(msg.data);
    });

    ws.on('outbound_sent', (evt) => {
        markPendingDelivered(evt?.clientMessageId || '');
    });

    ws.on('photo', (msg) => {
        hideTyping();
        const role = msg.role === 'user' ? 'user' : 'assistant';
        const sender = role === 'user'
            ? getSenderLabel('user', false, '', {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId: msg.sender_session_id || '',
            })
            : 'Ouroboros';
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}`;
        const timeFmt = formatMsgTime(msg.ts || new Date().toISOString());
        const timeHtml = timeFmt ? `<div class="msg-time" title="${escapeHtmlAttr(timeFmt.full)}">${escapeHtml(timeFmt.short)}</div>` : '';
        const captionHtml = msg.caption ? `<div class="message">${escapeHtml(msg.caption)}</div>` : '';
        const mime = /^image\/[a-z0-9.+-]+$/i.test(String(msg.mime || '')) ? String(msg.mime) : 'image/png';
        const imageBase64 = /^[A-Za-z0-9+/=\s]+$/.test(String(msg.image_base64 || ''))
            ? String(msg.image_base64 || '').replace(/\s+/g, '')
            : '';
        const imageUrl = imageBase64 ? `data:${mime};base64,${imageBase64}` : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            ${captionHtml}
            <div class="message"><img class="chat-photo" src="${escapeHtmlAttr(imageUrl)}" alt="Photo attachment"></div>
            ${timeHtml}
        `;
        const img = bubble.querySelector('.chat-photo');
        if (img && imageUrl) {
            img.addEventListener('click', () => window.open(imageUrl, '_blank'));
        }
        insertMessageNode(bubble);
        incrementUnreadIfNeeded();
    });

    ws.on('video', (msg) => {
        hideTyping();
        const role = msg.role === 'user' ? 'user' : 'assistant';
        const sender = role === 'user'
            ? getSenderLabel('user', false, '', {
                source: msg.source || '',
                senderLabel: msg.sender_label || '',
                senderSessionId: msg.sender_session_id || '',
            })
            : 'Ouroboros';
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${role}`;
        const timeFmt = formatMsgTime(msg.ts || new Date().toISOString());
        const timeHtml = timeFmt ? `<div class="msg-time" title="${escapeHtmlAttr(timeFmt.full)}">${escapeHtml(timeFmt.short)}</div>` : '';
        const captionHtml = msg.caption ? `<div class="message">${escapeHtml(msg.caption)}</div>` : '';
        const mime = /^video\/[a-z0-9.+-]+$/i.test(String(msg.mime || '')) ? String(msg.mime) : 'video/mp4';
        const videoBase64 = /^[A-Za-z0-9+/=\s]+$/.test(String(msg.video_base64 || ''))
            ? String(msg.video_base64 || '').replace(/\s+/g, '')
            : '';
        const videoUrl = videoBase64 ? `data:${mime};base64,${videoBase64}` : '';
        bubble.innerHTML = `
            <div class="sender">${escapeHtml(sender)}</div>
            ${captionHtml}
            <div class="message"><video class="chat-video" src="${escapeHtmlAttr(videoUrl)}" controls></video></div>
            ${timeHtml}
        `;
        insertMessageNode(bubble);
        incrementUnreadIfNeeded();
    });

    let wsHasConnectedOnce = false;

    ws.on('open', () => {
        setStatus('online', 'Онлайн');
        refreshHeaderControlState(true);
        const reconnectBanner =
            pendingReconnectBannerText
            || (wsHasConnectedOnce ? '♻️ Соединение установлено' : '');
        const shouldClearReconnectParams = Boolean(pendingReconnectBannerText);
        pendingReconnectBannerText = '';
        const isReconnect = wsHasConnectedOnce;
        wsHasConnectedOnce = true;
        updateMessagesPadding();
        syncHistory({ includeUser: !historyLoaded, fromReconnect: isReconnect })
            .then((hasMessages) => {
                if (!hasMessages) ensureWelcomeMessage();
                if (reconnectBanner) {
                    addMessage(reconnectBanner, 'system', false, null, false, { ephemeral: true, systemType: 'reconnect' });
                    if (shouldClearReconnectParams) clearPendingReconnectBanner();
                }
            })
            .catch(() => {
                if (reconnectBanner) {
                    addMessage(reconnectBanner, 'system', false, null, false, { ephemeral: true, systemType: 'reconnect' });
                    if (shouldClearReconnectParams) clearPendingReconnectBanner();
                }
            });
    });

    ws.on('close', () => {
        hideTyping();
        setStatus('offline', 'Переподключение...');
        syncHeaderControlState({ spent_usd: 0, budget_limit: 10, budget_text: 'Подключение...' });
    });
}
