import { escapeHtmlAttr, escapeHtmlText as escapeHtml, formatUsdWhole, renderMarkdown } from './utils.js';
import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { showToast } from './toast.js';
import { apiFetch } from './api_client.js';
import {
    getLogTaskGroupId,
    isGroupedTaskEvent,
    normalizeLogTs,
    summarizeChatLiveEvent,
} from './log_events.js';

const CHAT_STORAGE_KEY = 'ouro_chat';
const CHAT_INPUT_HISTORY_KEY = 'ouro_chat_input_history';
const CHAT_SESSION_ID_KEY = 'ouro_chat_session_id';
const PLAN_PREFIX = 'Please do multi-model planning (plan_task tool) and web-search before answering or starting this task:\n\n';

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
            title: 'Chat',
            icon: PAGE_ICONS.chat,
            variant: 'overlay',
            className: 'chat-page-header',
            actionsHtml: `
                <div class="chat-action-menu" id="chat-action-menu">
                    <span id="chat-status" class="status-badge offline">Connecting...</span>
                    <button class="chat-action-trigger" id="chat-action-trigger" type="button" title="Chat actions" aria-label="Chat actions" aria-expanded="false" aria-haspopup="menu">
                        <img class="chat-action-trigger-icon" src="/static/icons/settings-cog.svg" alt="">
                    </button>
                    <div class="chat-header-actions chat-action-dropdown" id="chat-header-actions" role="menu">
                        <button class="chat-header-btn" type="button" data-chat-command="evolve" role="menuitem" title="Toggle evolution mode"><span class="chat-action-icon" aria-hidden="true"><img src="/static/icons/evolution.svg" alt=""></span><span>Эволюция</span></button>
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
                <button class="chat-attach-btn" id="chat-attach" type="button" title="Attach file">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                </button>
                <input type="file" id="chat-file-input" class="chat-file-input-hidden" accept="*/*">
                <textarea id="chat-input" placeholder="Message Ouroboros..." rows="1" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
                <div class="chat-send-group">
                    <button class="chat-send-inline" id="chat-send" title="Send message">Send</button>
                    <button class="chat-send-chevron" id="chat-send-chevron" type="button" title="More send options" aria-label="More send options">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                    <div class="chat-send-dropdown" id="chat-send-dropdown" role="menu">
                        <button class="chat-send-dropdown-item" id="chat-dropdown-send" role="menuitem">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
                            Send
                        </button>
                        <button class="chat-send-dropdown-item" id="chat-dropdown-plan" role="menuitem">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/></svg>
                            Plan
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
    let pendingAttachment = null;

    // Shared stager: paperclip change handler AND clipboard paste both go through here
    // so the attachment badge / removal UI / upload-on-Send semantics are identical.
    function stagePendingFile(file) {
        if (!file) return;
        pendingAttachment = { file, display_name: file.name };
        attachmentPreview.classList.add('visible');
        attachmentPreview.innerHTML = `
            <span class="attach-badge">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="13 2 13 9 20 9"/></svg>
                <span class="attach-name">${escapeHtml(file.name)}</span>
                <button class="attach-remove" type="button" title="Remove">×</button>
            </span>
        `;
        requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
        attachmentPreview.querySelector('.attach-remove').addEventListener('click', () => {
            pendingAttachment = null;
            attachmentPreview.classList.remove('visible');
            attachmentPreview.innerHTML = '';
            requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
        });
    }

    attachBtn.addEventListener('click', () => fileInput.click());

    // Stage the selected File object locally — no server upload until sendMessage().
    // This avoids orphan files, race conditions with fast-send, and network usage for unsent files.
    fileInput.addEventListener('change', () => {
        const file = fileInput.files[0];
        if (!file) return;
        fileInput.value = '';
        stagePendingFile(file);
    });

    // Clipboard image paste: scan clipboardData.items for image/*, wrap as File via
    // getAsFile(), and route through the same stagePendingFile() path the paperclip
    // uses. preventDefault() runs ONLY when an image is matched so non-image clipboard
    // payloads (text, formatted text) still paste natively into the textarea. The
    // generated filename uses a unix timestamp + the MIME-derived extension so each
    // paste is a distinct attachment if the user pastes several in a row.
    input.addEventListener('paste', (e) => {
        const items = e.clipboardData && e.clipboardData.items;
        if (!items) return;
        for (let i = 0; i < items.length; i += 1) {
            const item = items[i];
            if (item && item.kind === 'file' && typeof item.type === 'string' && item.type.startsWith('image/')) {
                const blob = item.getAsFile();
                if (!blob) continue;
                e.preventDefault();
                const ext = (item.type.split('/')[1] || 'png').split(';')[0].trim() || 'png';
                const ts = Date.now();
                const safeBlob = blob instanceof File
                    ? new File([blob], `clipboard-${ts}.${ext}`, { type: blob.type })
                    : new File([blob], `clipboard-${ts}.${ext}`, { type: item.type });
                stagePendingFile(safeBlob);
                return;
            }
        }
    });

    // Set to true during syncHistory pass 1 to suppress premature DOM insertion of
    // live cards.  Cards are inserted in pass 2 at the correct chronological position.
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
    // Task ids that have been fully cleaned up (DOM removed, state freed).
    // Checked in syncHistory to prevent retired tasks from being recreated.
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
        if (reason === 'sha-change') return '♻️ Restart complete';
        if (reason) return '♻️ Reconnected';
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
            const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
            const todayStr = now.toDateString();
            const yesterday = new Date(now);
            yesterday.setDate(now.getDate() - 1);
            let short;
            if (d.toDateString() === todayStr) short = hhmm;
            else if (d.toDateString() === yesterday.toDateString()) short = `Yesterday, ${hhmm}`;
            else short = `${months[d.getMonth()]} ${d.getDate()}, ${hhmm}`;
            const full = `${months[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()} at ${hhmm}`;
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
            return opts.senderLabel || 'You';
        }
        if (role === 'system') {
            if (systemType === 'task_summary') return '📋 Task Summary';
            if (systemType === 'skill_review') return '📋 Skill Review';
            return '📋 System';
        }
        if (isProgress) return '💬 Thought';
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
                        <span class="skill-review-toggle-label">Show review</span>
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
        // Update budget pill
        const spent = data?.spent_usd || 0;
        const limit = data?.budget_limit || 10;
        const budgetText = document.getElementById('chat-budget-text');
        const budgetFill = document.getElementById('chat-budget-bar-fill');
        const pct = Math.min(100, (spent / limit) * 100);
        if (budgetText) budgetText.textContent = `${formatUsdWhole(spent)} / ${formatUsdWhole(limit)}`;
        if (budgetFill) budgetFill.style.width = `${pct}%`;
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

    function isNearBottom(threshold = NEAR_BOTTOM_THRESHOLD_PX) {
        const remaining = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
        return remaining <= threshold;
    }

    function insertMessageNode(node, options = {}) {
        if (!node) return;
        const shouldStick = Boolean(options.forceStick) || isNearBottom();
        if (node.parentNode === messagesDiv) {
            if (shouldStick) messagesDiv.scrollTop = messagesDiv.scrollHeight;
            return;
        }
        const typing = document.getElementById('typing-indicator');
        if (typing && typing.parentNode === messagesDiv) messagesDiv.insertBefore(node, typing);
        else messagesDiv.appendChild(node);
        if (shouldStick) messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function shouldAlwaysShowTaskCard(taskId = '') {
        return taskId === 'bg-consciousness';
    }

    function isTerminalTaskPhase(phase = '') {
        return phase === 'done' || phase === 'lifecycle_error';
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
            // After the cleanup delay, history sync has already materialized
            // the durable transcript.  Remove the live card DOM node (if it
            // is still a finished card in the chat, it has been superseded by
            // the regular assistant/summary bubbles added by syncHistory) and
            // free the backing JS arrays to prevent unbounded memory growth.
            // Keep the DOM node, backing arrays, and liveCardRecords entry intact
            // so the user keeps seeing the card with all its interactive expand/
            // collapse toggles still functional.  Only add to retiredTaskIds so
            // that routine incremental syncHistory() calls (fired after each new
            // task) do NOT re-build the card from history mid-session.
            // retiredTaskIds is cleared on first-load and on reconnect/restart,
            // so cards are fully reconstructed from progress.jsonl after a page
            // reload or soft restart.
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
        if (!(taskState.forceCard || taskState.toolCalls > 1 || shouldAlwaysShowTaskCard(taskState.taskId))) {
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

    // Task ids that represent reusable logical slots (multiple independent cycles).
    const REUSABLE_TASK_IDS = new Set(['bg-consciousness', 'active']);

    function queueTaskLiveUpdate(summary, taskId, ts, dedupeKey = '') {
        const resolvedTaskId = taskId || activeLiveGroupId || '';
        if (!resolvedTaskId) return;
        const taskState = getTaskUiState(resolvedTaskId, true);
        if (!taskState) return;
        if (taskState.completed && !isTerminalTaskPhase(summary.phase || '')) {
            // For reusable logical ids, a new non-terminal event means a new cycle
            // has started.  Reset UI state rather than dropping the event silently.
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

    function createLiveCardRecord(groupId = '') {
        const normalizedGroupId = groupId || `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const root = document.createElement('div');
        root.className = 'chat-live-card';
        root.dataset.finished = '0';
        root.dataset.expanded = '0';
        root.innerHTML = `
            <button type="button" class="chat-live-summary-button" data-live-summary-button>
                <div class="chat-live-summary">
                    <div class="chat-live-summary-main">
                        <span class="chat-live-phase working" data-live-phase>Working</span>
                        <div class="chat-live-typing" data-live-typing aria-hidden="true">
                            <span></span><span></span><span></span>
                        </div>
                        <span class="chat-live-title" data-live-title>Waiting for work</span>
                    </div>
                    <div class="chat-live-summary-side">
                        <span class="chat-live-count" data-live-count hidden>2 notes</span>
                        <span class="chat-live-toggle" data-live-toggle>Show details</span>
                        <svg class="chat-live-chevron" width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
                            <path d="M5 7.5 10 12.5 15 7.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"></path>
                        </svg>
                    </div>
                </div>
                <div class="chat-live-meta" data-live-meta></div>
            </button>
            <div class="chat-live-timeline" data-live-timeline></div>
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
            // Set to true when syncLiveCardLayout() was skipped because the chat
            // page was hidden; re-synced on the next ouro:page-shown / visibilitychange.
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
        record.titleEl.textContent = 'Working...';
        record.phaseEl.dataset.phase = 'working';
        record.phaseEl.textContent = 'Working';
        record.phaseEl.className = 'chat-live-phase working';
        record.countEl.hidden = true;
        record.countEl.textContent = '0 notes';
        record.metaEl.innerHTML = '';
        record.timelineEl.innerHTML = '';
        record.root.style.minHeight = '';
        record.root.dataset.finished = '0';
        setLiveCardTypingVisible(record, true);
        setLiveCardExpanded(record, false);
    }

    function ensureLiveCardVisible(record, { suppressDomInsert = false } = {}) {
        if (!suppressDomInsert && !_syncPass1Active) insertMessageNode(record.root);
    }

    function formatLiveCardPhaseLabel(phase) {
        if (phase === 'thinking') return 'Thinking';
        if (phase === 'working') return 'Working';
        if (phase === 'done') return 'Done';
        if (phase === 'warn') return 'Notice';
        if (phase === 'error' || phase === 'timeout') return 'Issue';
        if (!phase) return 'Working';
        return phase.charAt(0).toUpperCase() + phase.slice(1);
    }

    function setLiveCardExpanded(record, expanded) {
        if (!record?.root) return;
        record.root.dataset.expanded = expanded ? '1' : '0';
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
        record.toggleEl.textContent = record.root.dataset.expanded === '1' ? 'Hide details' : 'Show details';
    }

    const TIMELINE_MAX_HEIGHT = 420;

    function syncLiveCardLayout(record) {
        if (!record?.root || !record.summaryButtonEl) return;
        // If the chat page is not currently active (e.g. user is on another tab
        // inside the SPA, or the browser tab is backgrounded), getBoundingClientRect()
        // returns {height:0} and would set minHeight=0, collapsing the card to a
        // pixel-thin sliver.  Skip the geometry update and flag the record so we
        // re-sync when the chat page becomes visible again.
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

    // Re-sync layout for all connected live cards when the chat page becomes visible.
    // Covers two cases: (1) SPA navigation back to Chat tab, (2) browser tab un-hidden.
    window.addEventListener('ouro:page-shown', (event) => {
        if (event?.detail?.page !== 'chat') return;
        for (const record of liveCardRecords.values()) {
            if (record?.root?.isConnected) syncLiveCardLayout(record);
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
                >
                    <span class="chat-live-line-head">${headContent}</span>
                    <span class="chat-live-line-expand-label">${expanded ? 'Collapse' : 'Expand'}</span>
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
                ${displayBody ? `<div class="chat-live-line-body">${renderMarkdown(displayBody)}</div>` : ''}
            </div>
        `;
    }

    // Full rebuild — used for expand/collapse toggles and initial render.
    function renderLiveCardTimeline(record) {
        record.timelineEl.innerHTML = record.items.map((item) => buildTimelineItemHtml(item, record)).join('');
    }

    // Incremental: append a new item without touching existing DOM nodes.
    function appendTimelineItem(item, record) {
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const node = wrapper.firstElementChild;
        if (node) {
            record.timelineEl.appendChild(node);
            // Auto-scroll to latest item when expanded.
            if (record.root.dataset.expanded === '1') {
                record.timelineEl.scrollTop = record.timelineEl.scrollHeight;
            }
        }
    }

    // Patch the last DOM node when an existing item is updated (dedup / count bump).
    function patchLastTimelineItem(item, record) {
        const lastEl = record.timelineEl.lastElementChild;
        if (!lastEl) return renderLiveCardTimeline(record);
        const wrapper = document.createElement('div');
        wrapper.innerHTML = buildTimelineItemHtml(item, record).trim();
        const newNode = wrapper.firstElementChild;
        if (newNode) record.timelineEl.replaceChild(newNode, lastEl);
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
        if (record.finished && !isTerminalTaskPhase(nextPhase)) {
            return;
        }

        activeLiveGroupId = nextGroupId;
        ensureLiveCardVisible(record, { suppressDomInsert });
        record.updates += 1;
        const wasFinished = record.finished;
        record.finished = isTerminalTaskPhase(nextPhase);
        record.root.dataset.finished = record.finished ? '1' : '0';
        const headline = summary.headline || 'Working...';
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

        const syntheticKey = summary.dedupeKey || dedupeKey || `${summary.phase || 'working'}|${headline}|${summary.body || ''}`;
        const shouldRenderLine = summary.visible !== false && Boolean(headline || summary.body);
        // Track how the timeline should be updated: 'none' | 'patch-last' | 'append'
        let timelineUpdate = 'none';
        if (shouldRenderLine) {
            const last = record.items[record.items.length - 1];
            if (last && last.dedupeKey === syntheticKey) {
                last.count += 1;
                last.ts = ts || last.ts;
                last.fullHeadline = summary.fullHeadline || last.fullHeadline || last.headline;
                last.fullBody = summary.fullBody || last.fullBody || last.body;
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
        record.countEl.textContent = `${record.items.length} notes`;
        record.metaEl.innerHTML = [
            nextGroupId === 'bg-consciousness' ? 'Background thinking' : '',
            ts ? `Latest ${ts}` : '',
        ].filter(Boolean).map((item) => `<span class="chat-live-meta-text">${escapeHtml(item)}</span>`).join('');
        // Incremental DOM update: append new items or patch the last one.
        // Full rebuild is reserved for expand/collapse toggles.
        const lastItem = record.items[record.items.length - 1];
        if (timelineUpdate === 'append' && lastItem) {
            appendTimelineItem(lastItem, record);
        } else if (timelineUpdate === 'patch-last' && lastItem) {
            patchLastTimelineItem(lastItem, record);
        }
        if (!suppressDomInsert && !_syncPass1Active) insertMessageNode(record.root);
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
            setStatus(summary.phase === 'error' || summary.phase === 'timeout' ? 'error' : 'online', summary.phase === 'error' || summary.phase === 'timeout' ? 'Attention' : 'Online');
        } else {
            setLiveCardTypingVisible(record, true);
            setStatus('thinking', 'Working...');
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
        // Reset status badge when no live cards remain active
        if (!hasActiveLiveCard()) {
            setStatus(activePhase === 'error' || activePhase === 'timeout' ? 'error' : 'online',
                      activePhase === 'error' || activePhase === 'timeout' ? 'Attention' : 'Online');
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
        const doneHeadline = (record && record.lastHumanHeadline) || 'Done';
        applyLiveCardState(
            {
                phase: 'done',
                headline: doneHeadline,
                visible: false,
                human: false,
                promote: true,
            },
            taskId,
            normalizeLogTs(msg.ts || new Date().toISOString()),
            `task_done|${taskId}`,
            { suppressDomInsert },
        );
        finishLiveCard(taskId, 'done');
        scheduleTaskUiCleanup(taskState);
    }

    function updateLiveCardFromProgressMessage(msg) {
        const taskId = msg?.task_id || activeLiveGroupId || '';
        if (!taskId) return;
        // Progress messages are user-facing status updates (e.g. "🔍 Searching...")
        // that should always be visible — force the live card open immediately.
        // Do not force-open cards for already-completed tasks (history replay).
        const taskState = getTaskUiState(taskId, true);
        if (taskState && !taskState.completed) taskState.forceCard = true;
        const summary = summarizeChatLiveEvent({
            type: 'send_message',
            is_progress: true,
            content: msg?.content || msg?.text || '',
            text: msg?.content || msg?.text || '',
            task_id: taskId,
        });
        if (!summary) return;
        queueTaskLiveUpdate(summary, taskId, normalizeLogTs(msg.ts || new Date().toISOString()), summary.dedupeKey || '');
    }

    function updateLiveCardFromLogEvent(evt) {
        if (!evt || !isGroupedTaskEvent(evt)) return;
        const taskId = getLogTaskGroupId(evt) || activeLiveGroupId || '';
        if (!taskId) return;
        const eventType = evt.type || evt.event || '';
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
        const pendingHtml = pending ? `<div class="msg-pending">Queued until reconnect</div>` : '';
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
                if (label) label.textContent = expanded ? 'Show review' : 'Hide review';
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
        addMessage('Ouroboros has awakened', 'assistant', false, null, false, { ephemeral: true });
    }

    async function syncHistory({ includeUser = false, fromReconnect = false } = {}) {
        if (historySyncPromise) {
            // A sync is already in flight.  If this call came from a reconnect we must
            // NOT lose the intent: after the in-flight sync settles we need a fresh run
            // that clears retiredTaskIds.  Record the pending request and chain it.
            if (fromReconnect) pendingReconnectSync = true;
            return historySyncPromise;
        }
        historySyncPromise = (async () => {
            try {
                const resp = await apiFetch('/api/chat/history?limit=1000', { cache: 'no-store' });
                if (!resp.ok) return false;
                const data = await resp.json();
                const messages = Array.isArray(data.messages) ? data.messages : [];

                // On a full reconnect / initial page load, server history is the
                // authoritative source of truth for what cards should be visible.
                // Clear retiredTaskIds so that previously-cleaned-up cards (e.g.
                // from the pre-restart session) can be reconstructed from history.
                // We do this on:
                //   (a) first load (historyLoaded === false) — hard restart / fresh page load
                //   (b) fromReconnect === true — soft restart / WS reconnect after same-SHA
                //       restart, where historyLoaded stays true across the reconnect
                // We do NOT clear on routine scheduleHistorySync() calls (fired 700ms after
                // task completion) to avoid resurrecting already-cleaned-up cards mid-session.
                if (!historyLoaded || fromReconnect) retiredTaskIds.clear();

                // Two-pass processing: progress/summary first, then regular messages.
                // This guarantees live cards are built before finishLiveCard() is called,
                // so progress bubbles are never discarded due to taskState.completed=true.

                // Pass 1: progress messages and task summaries (build card timelines).
                // DOM insertion is SUPPRESSED here (_syncPass1Active=true blocks all
                // ensureLiveCardVisible/insertMessageNode calls) — cards are inserted in
                // pass 2 just before their corresponding assistant reply so the DOM order
                // is correct.
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
                        // Force the card visible for historical tasks only when the task
                        // was non-trivial (had tool calls or multiple rounds).  Trivial
                        // tasks (simple replies) should not show a card at all.
                        const hadToolCalls = (msg.tool_calls || 0) > 0;
                        const hadMultipleRounds = (msg.rounds || 0) > 1;
                        if (hadToolCalls || hadMultipleRounds) {
                            const taskState = getTaskUiState(taskId, true);
                            if (taskState) taskState.forceCard = true;
                        }
                        // suppressDomInsert=true: build the card record + timeline
                        // in memory only; pass 2 will insert it at the right position.
                        appendTaskSummaryToLiveCard(msg, { suppressDomInsert: true });
                    }
                } } finally { _syncPass1Active = false; }

                // Pass 2: regular messages (assistant replies, user messages, system bubbles).
                // finishLiveCard() is called here, after the card timeline is already populated.
                // Live cards are inserted when the FIRST message for each taskId is encountered
                // (progress or reply), so the card appears at the correct chronological position
                // regardless of whether the task has finished or is still ongoing.
                const insertedCardTaskIds = new Set();
                function insertCardIfNeeded(taskId) {
                    if (!taskId || insertedCardTaskIds.has(taskId)) return;
                    insertedCardTaskIds.add(taskId);
                    const rec = liveCardRecords.get(taskId);
                    if (rec && rec.root && !rec.root.isConnected) {
                        insertMessageNode(rec.root);
                    }
                }
                for (const msg of messages) {
                    const taskId = msg.task_id || '';
                    if (!includeUser && msg.role === 'user') continue;
                    if (msg.is_progress) {
                        // Insert the card at the first progress message position so
                        // ongoing/failed tasks also appear in the right spot.
                        insertCardIfNeeded(taskId);
                        continue;
                    }
                    if (msg.system_type === 'task_summary') continue;
                    if (taskId && (msg.role === 'assistant' || msg.role === 'system')) {
                        insertCardIfNeeded(taskId);
                        finishLiveCard(taskId);
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
                // Ensure any still-disconnected live cards (e.g. in-progress tasks with no
                // reply yet, or background-consciousness cards) are appended at the end so
                // they remain visible after a mid-task reload.  Skip cards that were never
                // made visible (trivial tasks with 0 tool calls) to avoid a cluster of
                // invisible placeholder nodes appearing at the bottom of the chat.
                for (const [tid, rec] of liveCardRecords) {
                    if (rec && rec.root && !rec.root.isConnected && !retiredTaskIds.has(tid)) {
                        const ts = taskUiStates.get(tid);
                        if (ts && !ts.cardVisible && ts.completed) continue;
                        insertMessageNode(rec.root);
                    }
                }

                // After first load: if there is an active in-progress task with a visible
                // live card but no assistant reply yet, show the typing indicator so the
                // user knows work is ongoing (e.g. page reload mid-task).
                if (!historyLoaded) {
                    const hasOngoingTask = Array.from(liveCardRecords.values()).some(
                        (record) => record?.root?.isConnected && !record.finished
                    );
                    if (hasOngoingTask) showTyping();
                }

                // Seed inputHistory from server-side chat history on the FIRST
                // successful server sync of this page lifetime.  This makes ArrowUp
                // recall include Telegram messages and messages sent from other
                // browser sessions that never went through the local rememberInput()
                // path.  PLAN_PREFIX is stripped so plan-mode preambles don't
                // pollute recall.
                //
                // The seeding is gated on a one-shot inputHistorySeededFromServer
                // flag (NOT on !historyLoaded) so it still fires when the initial
                // /api/chat/history fetch failed and historyLoaded was already set
                // true by the sessionStorage-fallback bootstrap IIFE.  Subsequent
                // WS reconnects deliberately do NOT re-seed: that would reset
                // inputHistoryIndex mid-scrub if the user is holding ArrowUp while
                // the socket reconnects.  Tradeoff: new Telegram/other-session
                // messages that arrive while this tab stays open surface in recall
                // only after the next full page reload.
                //
                // Merge strategy: server history is chronologically older, local
                // session history is newer.  We build a combined list [server...,
                // local...] and deduplicate from the END (most-recent wins) so that
                // the most recent ArrowUp entry is always the last thing sent from
                // this session, and older server entries fill slots below it.
                if (!inputHistorySeededFromServer) {
                    const serverTexts = [];
                    for (const msg of messages) {
                        if (msg.role !== 'user') continue;
                        let text = (msg.text || '').trim();
                        if (text.startsWith(PLAN_PREFIX)) text = text.slice(PLAN_PREFIX.length).trimStart();
                        if (text) serverTexts.push(text);
                    }
                    // Merge strategy: server messages are chronologically older,
                    // local session messages are newer.  Build combined [server...,
                    // local...] and deduplicate from the END (most-recent wins).
                    const combined = [...serverTexts, ...inputHistory];
                    const deduped = [];
                    const seen = new Set();
                    for (let i = combined.length - 1; i >= 0; i--) {
                        if (!seen.has(combined[i])) {
                            deduped.unshift(combined[i]);
                            seen.add(combined[i]);
                        }
                    }
                    // Replace in-place, cap at 50 (keep most recent = tail)
                    inputHistory.length = 0;
                    inputHistory.push(...deduped.slice(-50));
                    saveInputHistory(inputHistory);
                    inputHistoryIndex = inputHistory.length;
                    inputHistorySeededFromServer = true;
                }

                const wasFirstLoad = !historyLoaded;
                historyLoaded = true;
                // On first load (page open / restart), scroll to the latest
                // message. On subsequent reconnect syncs, only scroll if the
                // user was already near the bottom to avoid hijacking scroll
                // position when they are reading older messages.
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
                // If a reconnect-triggered sync arrived while we were in-flight, run it
                // now so retiredTaskIds.clear() executes with the latest server state.
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
        const caretAtEnd = input.selectionEnd >= input.value.length - 1;
        const previousScrollTop = input.scrollTop;
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 120) + 'px';
        input.scrollTop = caretAtEnd ? input.scrollHeight : previousScrollTop;
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
        if (!text && !pendingAttachment) return;
        if (pendingAttachment) {
            // Upload the staged file now, right before sending the chat message.
            // Requires live WebSocket; if offline, upload is rejected to avoid
            // the unsolvable race between queued message delivery and orphan cleanup.
            if (ws.ws?.readyState !== WebSocket.OPEN) {
                showToast('Cannot attach file while offline. Reconnect and try again.', 'error');
                return;
            }
            const staged = pendingAttachment;
            setSendBusy(true, 'Uploading');
            try {
                const formData = new FormData();
                formData.append('file', staged.file);
                const resp = await apiFetch('/api/chat/upload', { method: 'POST', body: formData });
                const data = await resp.json();
                if (!resp.ok || !data.ok) {
                    showToast('Upload failed: ' + (data.error || resp.statusText), 'error');
                    return;  // pendingAttachment and preview remain — user can retry
                }
                // Upload succeeded — clear the staged attachment now that it's on the server
                pendingAttachment = null;
                attachmentPreview.classList.remove('visible');
                attachmentPreview.innerHTML = '';
                requestAnimationFrame(() => updateMessagesPadding({ preserveStickiness: false }));
                text += (text ? '\n\n' : '') + `[Attached file: ${data.display_name || staged.display_name} saved to ${data.path}]`;
            } catch (e) {
                showToast('Upload error: ' + e.message, 'error');
                return;  // pendingAttachment and preview remain — user can retry
            } finally {
                setSendBusy(false);
            }
        }
        if (!text) return;
        // Recall history always uses the raw user text (no prefix pollution on ArrowUp).
        rememberInput(text);
        input.value = '';
        // Apply planning prefix to wire content only; display text stays clean.
        // Slash commands are always sent verbatim regardless of planMode.
        const wireText = (planMode && !text.startsWith('/')) ? PLAN_PREFIX + text : text;
        const result = ws.send({
            type: 'chat',
            content: wireText,
            sender_session_id: chatSessionId,
        });
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

    // Dropdown toggle helpers
    // Send-mode state — stored on DOM so CSS can key off data-send-mode attribute.
    // Default: 'send'. Modes: 'send' | 'plan'.
    const sendGroup = document.querySelector('.chat-send-group');

    function setSendMode(mode) {
        sendGroup.dataset.sendMode = mode;
        sendBtn.textContent = mode === 'plan' ? 'Plan' : 'Send';
        sendBtn.title = mode === 'plan' ? 'Send with planning prefix' : 'Send message';
        // Mark the active item in the dropdown for visual feedback.
        dropdownSend.dataset.modeActive = mode === 'send' ? 'true' : 'false';
        dropdownPlan.dataset.modeActive = mode === 'plan' ? 'true' : 'false';
    }

    function setSendBusy(busy, label = '') {
        sendGroup.dataset.busy = busy ? '1' : '0';
        sendBtn.disabled = busy;
        chevronBtn.disabled = busy;
        if (busy) {
            sendBtn.textContent = label || 'Sending';
            sendBtn.title = label || 'Sending';
        } else {
            setSendMode(sendGroup.dataset.sendMode || 'send');
        }
    }

    // Initialise to send mode.
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
    // Dropdown items now SWITCH the mode instead of immediately sending.
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

    // Use explicit arrow functions to avoid MouseEvent being passed as planMode arg.
    // Both send paths derive planMode from the DOM dataset — single source of truth.
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
    // v5.7.0+: #chat-input-area is an absolute-positioned translucent overlay.
    // The bottom reserve is dynamic via --chat-input-reserve so normal
    // one-line composition does not leave a huge blank gap, while multiline
    // textareas / attachment preview still reserve enough scroll space.
    // We set a CSS variable (not paddingBottom directly) so the design-system
    // "no separate fade layer" rule remains intact and CSS owns the actual
    // padding expression, including mobile safe-area.
    function scrollToBottom() {
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
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
        if (command === 'panic' && confirm('Kill all workers immediately?')) {
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
            if (isNearBottom()) messagesDiv.scrollTop = messagesDiv.scrollHeight;
        }
        setStatus('thinking', 'Thinking...');
    }

    function hideTypingIndicatorOnly() {
        typingEl.style.display = 'none';
    }

    function hideTyping() {
        hideTypingIndicatorOnly();
        if (statusBadge && ['Thinking...', 'Working...'].includes(statusBadge.textContent)) {
            setStatus('online', 'Online');
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

    let wsHasConnectedOnce = false;

    ws.on('open', () => {
        setStatus('online', 'Online');
        refreshHeaderControlState(true);
        const reconnectBanner =
            pendingReconnectBannerText
            || (wsHasConnectedOnce ? '♻️ Reconnected' : '');
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
        setStatus('offline', 'Reconnecting...');
    });
}
