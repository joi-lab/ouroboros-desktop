import { formatUsd4 } from './utils.js';

export const LOG_CATEGORIES = {
    tools: { label: 'Tools', color: 'var(--blue)' },
    llm: { label: 'LLM', color: 'var(--accent)' },
    errors: { label: 'Errors', color: 'var(--red)' },
    tasks: { label: 'Tasks', color: 'var(--amber)' },
    system: { label: 'System', color: 'var(--text-muted)' },
    consciousness: { label: 'Consciousness', color: 'var(--accent)' },
};

export function categorizeLogEvent(evt) {
    const t = evt.type || evt.event || '';
    if (evt.is_progress) {
        return evt.task_id === 'bg-consciousness' ? 'consciousness' : 'tasks';
    }
    if (t.includes('error') || t.includes('crash') || t.includes('fail')) return 'errors';
    if (t.includes('llm') || t.includes('model')) return 'llm';
    if (t.includes('tool') || evt.tool) return 'tools';
    if (t.includes('task') || t.includes('evolution') || t.includes('review')) return 'tasks';
    if (t.includes('consciousness') || t.includes('bg_')) return 'consciousness';
    return 'system';
}

export function normalizeLogTs(isoStr) {
    if (!isoStr) return '';
    try {
        const d = new Date(isoStr);
        if (Number.isNaN(d.getTime())) return '';
        return d.toLocaleTimeString([], { hour12: false });
    } catch {
        return '';
    }
}

function shortText(text, maxLen = 180) {
    const s = String(text || '').replace(/\s+/g, ' ').trim();
    if (!s) return '';
    return s.length > maxLen ? s.slice(0, maxLen - 3) + '...' : s;
}

function describeText(text, maxLen = 180) {
    const full = String(text || '').trim();
    if (!full) return { preview: '', full: '' };
    const previewSource = full.replace(/\s+/g, ' ');
    return {
        preview: previewSource.length > maxLen ? previewSource.slice(0, maxLen - 3) + '...' : previewSource,
        full,
    };
}

function subagentId(evt) {
    return String(evt.subagent_task_id || evt.task_id || '').trim();
}

function isSubagentEvent(evt) {
    return String(evt.delegation_role || '').toLowerCase() === 'subagent' || Boolean(evt.subagent_task_id);
}

export function formatLogMoney(value) {
    return formatUsd4(value);
}

export function formatLogDuration(sec) {
    const num = Number(sec);
    if (!Number.isFinite(num) || num < 0) return '';
    if (num >= 60) {
        const mins = Math.floor(num / 60);
        const rem = Math.round(num % 60);
        return `${mins}m ${rem}s`;
    }
    return `${num < 10 ? num.toFixed(1) : Math.round(num)}s`;
}

function formatLogTokens(evt) {
    const prompt = Number(evt.prompt_tokens || 0);
    const completion = Number(evt.completion_tokens || 0);
    if (!prompt && !completion) return '';
    return `${prompt}\u2192${completion} tok`;
}

function compactJson(value, maxLen = 220) {
    if (value == null) return '';
    let txt = '';
    try {
        txt = JSON.stringify(value);
    } catch {
        txt = String(value);
    }
    return shortText(txt, maxLen);
}

function extractCommandText(args) {
    if (!args || typeof args !== 'object') return '';
    const cmd = args.cmd;
    if (Array.isArray(cmd)) {
        return cmd.map((part) => String(part || '').trim()).filter(Boolean).join(' ');
    }
    if (typeof cmd === 'string') return cmd;
    return '';
}

function describeStartupChecks(checks) {
    if (!checks || typeof checks !== 'object') return '';
    const parts = [];
    for (const [key, value] of Object.entries(checks)) {
        if (value && typeof value === 'object' && value.status) {
            parts.push(`${key}:${value.status}`);
        }
    }
    return shortText(parts.join(' | '), 240);
}

function taskDoneFailure(evt) {
    const resultStatus = String(evt.result_status || '').toLowerCase();
    const artifactStatus = String(evt.artifact_bundle?.status || evt.artifact_status || '').toLowerCase();
    return ['failed', 'infra_failed'].includes(resultStatus) || artifactStatus === 'failed';
}

function taskDoneLabel(evt) {
    const reasonCode = evt.reason_code ? String(evt.reason_code) : '';
    if (taskDoneFailure(evt)) {
        return reasonCode ? `Failed: ${reasonCode}` : `Failed ${evt.task_type || 'task'}`;
    }
    return `Finished ${evt.task_type || 'task'}`;
}

export function summarizeLogEvent(evt) {
    const t = evt.type || evt.event || 'unknown';
    const view = (phase, headline, { body = '', meta = [], typeLabel = t } = {}) => ({
        typeLabel,
        phase,
        headline,
        body,
        meta: meta.filter(Boolean),
    });
    const taskMeta = (...items) => [evt.task_id ? `task=${evt.task_id}` : '', ...items];

    if (evt.is_progress || t === 'send_message') {
        if (isSubagentEvent(evt)) {
            const sid = subagentId(evt);
            const event = String(evt.subagent_event || 'update').toLowerCase();
            const role = String(evt.subagent_role || '').trim();
            return view(event === 'completed' ? 'done' : event === 'failed' || event === 'rejected' ? 'warn' : 'progress', `Subagent ${sid || 'child'} ${event}`, {
                body: shortText(String(evt.content || evt.text || '').replace(/^💬\s*/, ''), 240),
                meta: [
                    sid ? `task=${sid}` : '',
                    role ? `role=${role}` : '',
                    evt.parent_task_id ? `parent=${evt.parent_task_id}` : '',
                    evt.root_task_id ? `root=${evt.root_task_id}` : '',
                ],
            });
        }
        return view(
            evt.task_id === 'bg-consciousness' ? 'thought' : 'progress',
            shortText(String(evt.content || evt.text || '').replace(/^💬\s*/, ''), 240) || 'Progress update',
            { meta: [evt.task_id === 'bg-consciousness' ? 'background' : 'task'] },
        );
    }

    if (t === 'task_started') {
        return view('start', `Started ${evt.task_type || 'task'}`, {
            body: shortText(evt.task_text, 220),
            meta: taskMeta(evt.direct_chat ? 'chat' : 'queued'),
        });
    }

    if (t === 'task_received') {
        const task = evt.task || {};
        return view('queued', `Received ${task.type || 'task'}`, {
            body: shortText(task.text, 220),
            meta: [task.id ? `task=${task.id}` : '', task.text_len ? `${task.text_len} chars` : ''],
        });
    }

    if (t === 'context_building_started') {
        return view('context', 'Building context', { meta: taskMeta(evt.task_type || '') });
    }

    if (t === 'context_building_finished') {
        return view('ready', 'Context ready', {
            meta: taskMeta(
                evt.message_count != null ? `${evt.message_count} msgs` : '',
                Number.isFinite(Number(evt.budget_remaining_usd)) ? `$${Number(evt.budget_remaining_usd).toFixed(2)} left` : '',
            ),
        });
    }

    if (t === 'task_heartbeat') {
        return view(evt.phase || 'alive', 'Still working', {
            meta: taskMeta(evt.task_type || '', formatLogDuration(evt.runtime_sec)),
        });
    }

    if (t === 'llm_round_started') {
        return view('calling', `Calling ${evt.model || 'model'}`, {
            meta: taskMeta(
                evt.round ? `r${evt.round}` : '',
                evt.attempt ? `try ${evt.attempt}` : '',
                evt.reasoning_effort || '',
                evt.use_local ? 'local' : '',
            ),
        });
    }

    if (t === 'llm_round_finished' || t === 'llm_round') {
        return view('done', `LLM round ${evt.round || ''} finished`.trim(), {
            meta: taskMeta(
                evt.model || '',
                formatLogTokens(evt),
                formatLogMoney(evt.cost_usd || evt.cost),
                evt.response_kind === 'tool_calls' ? `${evt.tool_call_count || 0} tool calls` : evt.response_kind || '',
            ),
        });
    }

    if (t === 'llm_round_empty' || t === 'llm_empty_response') {
        return view('empty', 'Model returned empty response', {
            meta: taskMeta(evt.model || '', evt.round ? `r${evt.round}` : ''),
        });
    }

    if (t === 'llm_round_error' || t === 'llm_api_error') {
        return view('error', 'LLM call failed', {
            body: shortText(evt.error, 260),
            meta: taskMeta(evt.model || '', evt.round ? `r${evt.round}` : ''),
        });
    }

    if (t === 'llm_usage') {
        return view('usage', 'LLM usage recorded', {
            meta: taskMeta(
                evt.model || '',
                formatLogTokens(evt),
                formatLogMoney(evt.cost_usd || evt.cost),
                evt.category || '',
            ),
        });
    }

    if (t === 'tool_call_started') {
        return view('start', `Running ${evt.tool || 'tool'}`, {
            body: compactJson(evt.args, 260),
            meta: taskMeta(evt.timeout_sec ? `timeout ${evt.timeout_sec}s` : ''),
        });
    }

    if (t === 'tool_call_finished') {
        return view(evt.is_error ? 'error' : 'done', `${evt.tool || 'tool'} ${evt.is_error ? 'failed' : 'finished'}`, {
            body: shortText(evt.result_preview, 260),
            meta: taskMeta(formatLogDuration(evt.duration_sec)),
        });
    }

    if (t === 'tool_call_timeout' || t === 'tool_timeout') {
        return view('timeout', `${evt.tool || 'tool'} timed out`, {
            body: compactJson(evt.args, 220),
            meta: taskMeta(evt.timeout_sec ? `limit ${evt.timeout_sec}s` : '', formatLogDuration(evt.duration_sec)),
        });
    }

    if (t === 'tool_call' || evt.tool) {
        return view('result', `${evt.tool || 'tool'} result`, {
            body: shortText(evt.result_preview || compactJson(evt.args, 220), 260),
            meta: taskMeta(),
        });
    }

    if (t === 'task_metrics_event' || t === 'task_eval') {
        return view('metrics', 'Task metrics', {
            meta: taskMeta(
                evt.task_type || '',
                evt.result_status || '',
                evt.reason_code || '',
                formatLogDuration(evt.duration_sec),
                evt.tool_calls != null ? `${evt.tool_calls} tools` : '',
                evt.tool_errors ? `${evt.tool_errors} errors` : '',
                evt.response_len ? `${evt.response_len} chars` : '',
            ),
        });
    }

    if (t === 'task_done') {
        const resultStatus = evt.result_status ? String(evt.result_status) : '';
        const reasonCode = evt.reason_code ? String(evt.reason_code) : '';
        const artifactStatus = evt.artifact_bundle?.status || evt.artifact_status || '';
        return view(taskDoneFailure(evt) ? 'error' : 'done', taskDoneLabel(evt), {
            meta: taskMeta(
                resultStatus,
                reasonCode,
                artifactStatus ? `artifacts ${artifactStatus}` : '',
                formatLogMoney(evt.cost_usd || evt.cost),
                evt.total_rounds ? `${evt.total_rounds} rounds` : '',
                formatLogTokens(evt),
            ),
        });
    }

    if (t === 'startup_verification') {
        return view(Number(evt.issues_count || 0) > 0 ? 'warn' : 'ok', 'Startup verification', {
            body: describeStartupChecks(evt.checks),
            meta: [evt.git_sha ? String(evt.git_sha).slice(0, 8) : '', `${evt.issues_count || 0} issues`],
        });
    }

    if (t === 'worker_spawn_start') {
        return view('start', `Spawning ${evt.count || '?'} workers`, { meta: [evt.start_method || ''] });
    }

    if (t === 'worker_sha_verify') {
        return view(evt.ok ? 'ok' : 'warn', evt.ok ? 'Worker SHA verified' : 'Worker SHA mismatch', {
            meta: [
                evt.expected_sha ? `exp ${String(evt.expected_sha).slice(0, 8)}` : '',
                evt.observed_sha ? `got ${String(evt.observed_sha).slice(0, 8)}` : '',
                evt.worker_pid ? `pid ${evt.worker_pid}` : '',
            ],
        });
    }

    if (t === 'worker_boot') {
        return view('boot', 'Worker booted', {
            meta: [evt.pid ? `pid ${evt.pid}` : '', evt.git_sha ? String(evt.git_sha).slice(0, 8) : ''],
        });
    }

    if (t === 'deps_sync_ok') {
        return view('ok', 'Dependencies in sync', { meta: [evt.reason || '', shortText(evt.source, 60)] });
    }

    if (t === 'reset_unsynced_rescued_then_reset') {
        return view('warn', 'Recovered dirty worktree before restart', {
            meta: [
                evt.reason || '',
                evt.dirty_count != null ? `${evt.dirty_count} dirty` : '',
                evt.unpushed_count != null ? `${evt.unpushed_count} unpushed` : '',
            ],
        });
    }

    if (t === 'task_checkpoint') {
        const cpNum = evt.checkpoint_number || Math.floor((evt.round || 0) / 15);
        return view('thinking', `Checkpoint ${cpNum}`, {
            meta: taskMeta(
                evt.round ? `r${evt.round}` : '',
                evt.context_tokens ? `~${evt.context_tokens} tok` : '',
                formatLogMoney(evt.task_cost),
            ),
        });
    }

    if (t.includes('error') || t.includes('crash') || t.includes('fail')) {
        return view('error', t, {
            body: shortText(evt.error || evt.result_preview || evt.text || '', 260),
            meta: taskMeta(evt.tool ? `tool=${evt.tool}` : ''),
        });
    }

    return view('info', shortText(t, 120), {
        body: shortText(evt.text || evt.error || evt.result_preview || compactJson(evt.args || evt.task || evt.checks, 260), 260),
        meta: taskMeta(evt.model || '', formatLogMoney(evt.cost_usd || evt.cost)),
    });
}

function chatView({
    phase = 'working',
    headline = 'Working...',
    body = '',
    fullBody = '',
    fullHeadline = '',
    visible = false,
    promote = false,
    terminal = false,
    human = false,
    dedupeKey = '',
    meta = [],
} = {}) {
    const out = {
        phase,
        headline,
        body,
        visible,
        promote,
        terminal,
        human,
        dedupeKey,
    };
    if (fullBody) out.fullBody = fullBody;
    if (fullHeadline) out.fullHeadline = fullHeadline;
    if (Array.isArray(meta) && meta.length) out.meta = meta.filter(Boolean);
    return out;
}

export function summarizeChatLiveEvent(evt) {
    const t = evt.type || evt.event || 'unknown';
    const groupId = getLogTaskGroupId(evt);
    const progressText = describeText(String(evt.content || evt.text || '').replace(/^💬\s*/, ''), 240);
    const key = (...parts) => [t, groupId, ...parts].join(':');

    if (evt.lifecycle && typeof evt.lifecycle === 'object') {
        const lifecycle = evt.lifecycle;
        const status = String(lifecycle.status || '').toLowerCase();
        const stale = Boolean(lifecycle.stale);
        const phase = status === 'succeeded' ? 'done'
            : ['failed', 'cancelled', 'interrupted'].includes(status) ? 'lifecycle_error'
                : stale ? 'warn'
                    : 'working';
        const label = lifecycle.phase || status || 'working';
        const target = lifecycle.target ? `\`${lifecycle.target}\`` : 'skill';
        const headline = progressText.preview || `Skill ${lifecycle.kind || 'operation'}: ${target} — ${label}`;
        const body = stale
            ? (lifecycle.recovery_hint || 'Lifecycle work is still running; restart may be required.')
            : (lifecycle.error || lifecycle.message || '');
        return chatView({
            phase,
            headline,
            body: shortText(body, 220),
            fullHeadline: progressText.full || headline,
            fullBody: body,
            visible: true,
            promote: true,
            terminal: phase === 'done' || phase === 'lifecycle_error',
            human: true,
            dedupeKey: lifecycle.id ? `lifecycle:${lifecycle.id}:${status}:${label}:${stale ? 'stale' : 'fresh'}` : key(status, label),
        });
    }

    if ((evt.is_progress || t === 'send_message') && isSubagentEvent(evt)) {
        const sid = subagentId(evt);
        const event = String(evt.subagent_event || '').toLowerCase();
        const role = String(evt.subagent_role || '').trim();
        const status = String(evt.status || '').trim();
        const cost = formatLogMoney(evt.cost_usd || evt.cost);
        const resultText = describeText(evt.result || '', 320);
        const traceText = describeText(evt.trace_summary || '', 320);
        const errorText = describeText(evt.error || '', 220);
        const detailParts = [
            progressText.full,
            resultText.full ? `[RESULT]\n${resultText.full}` : '',
            traceText.full ? `[TRACE]\n${traceText.full}` : '',
            errorText.full ? `[ERROR]\n${errorText.full}` : '',
        ].filter(Boolean);
        const phase = ['completed'].includes(event) ? 'done'
            : ['failed', 'rejected', 'cancelled', 'interrupted'].includes(event) ? 'lifecycle_error'
                : event === 'scheduled' ? 'start'
                    : 'working';
        const label = event || 'update';
        return chatView({
            phase,
            headline: `Subagent ${sid || 'child'} ${label}`,
            body: progressText.preview || resultText.preview || errorText.preview || '',
            fullBody: detailParts.join('\n\n'),
            visible: true,
            promote: true,
            human: true,
            meta: [
                'subagent',
                role ? `role=${role}` : '',
                status ? `status=${status}` : '',
                cost ? `cost=${cost}` : '',
                evt.parent_task_id ? `parent=${evt.parent_task_id}` : '',
                evt.root_task_id ? `root=${evt.root_task_id}` : '',
            ],
            dedupeKey: `subagent:${sid}:${label}:${status}:${progressText.full || resultText.full || errorText.full || ''}`,
        });
    }

    if (evt.is_progress || t === 'send_message') {
        const lifecycleTerminal = String(evt.task_id || '').startsWith('skill_lifecycle_')
            && /\s—\s(completed|failed)\b/i.test(progressText.full);
        return chatView({
            phase: evt.task_id === 'bg-consciousness'
                ? 'thinking'
                : (lifecycleTerminal ? (/failed\b/i.test(progressText.full) ? 'lifecycle_error' : 'done') : 'working'),
            headline: progressText.preview || 'Working...',
            fullHeadline: progressText.full || '',
            visible: Boolean(progressText.preview),
            promote: true,
            human: true,
            dedupeKey: progressText.full ? `progress:${progressText.full}` : `progress:${evt.task_id || ''}`,
        });
    }

    if (t === 'task_started' || t === 'task_received') {
        return chatView({ headline: 'Working on it', promote: true, dedupeKey: key() });
    }

    if (t === 'context_building_started') {
        return chatView({ headline: 'Getting ready', promote: true, dedupeKey: key() });
    }

    if (t === 'context_building_finished') {
        return chatView({ headline: 'Looking through the context', dedupeKey: key() });
    }

    if (t === 'task_heartbeat') {
        return chatView({ headline: 'Still working', dedupeKey: key(evt.phase || '') });
    }

    if (t === 'llm_round_started') {
        return chatView({ phase: 'thinking', headline: 'Thinking', dedupeKey: key(evt.round || '', evt.attempt || '') });
    }

    if (t === 'tool_call_started') {
        return chatView({ headline: 'Working through the next step', dedupeKey: key(evt.tool || '') });
    }

    if (t === 'task_checkpoint') {
        // Not visible in chat live card — the emit_progress message is the visible source
        // for the chat timeline (avoids duplicate timeline entries). This event remains
        // visible in the Logs tab via summarizeLogEvent.
        const cpNum = evt.checkpoint_number || Math.floor((evt.round || 0) / 15);
        return chatView({
            phase: 'thinking',
            headline: `Checkpoint ${cpNum} — periodic self-check`,
            dedupeKey: key(cpNum),
        });
    }

    if (t === 'llm_round_error' || t === 'llm_api_error') {
        const errorText = describeText(evt.error, 220);
        return chatView({
            phase: 'error',
            headline: 'Ran into an issue while thinking',
            body: errorText.preview,
            fullBody: errorText.full,
            visible: true,
            promote: true,
            dedupeKey: key(evt.round || ''),
        });
    }

    if (t === 'tool_call_timeout' || t === 'tool_timeout') {
        return chatView({
            phase: 'error',
            headline: 'One of the steps took too long',
            visible: true,
            promote: true,
            dedupeKey: key(evt.tool || ''),
        });
    }

    if (t === 'tool_call_finished' && evt.is_error) {
        const commandText = describeText(extractCommandText(evt.args), 120);
        const errorResult = describeText(evt.result_preview || evt.error, 220);
        const bodyParts = [];
        const fullBodyParts = [];
        if (commandText.preview) bodyParts.push(`Command: ${commandText.preview}`);
        if (errorResult.preview) bodyParts.push(errorResult.preview);
        if (commandText.full) fullBodyParts.push(`Command: ${commandText.full}`);
        if (errorResult.full) fullBodyParts.push(errorResult.full);
        if (evt.status === 'non_zero_exit') {
            const exitCode = Number(evt.exit_code);
            return chatView({
                phase: 'warn',
                headline: `A command returned ${Number.isFinite(exitCode) ? `exit code ${exitCode}` : 'a non-zero exit code'}`,
                body: shortText(bodyParts.join(' '), 220),
                fullBody: fullBodyParts.join('\n\n'),
                visible: true,
                dedupeKey: key(evt.tool || '', evt.status || '', evt.exit_code || '', commandText.full || errorResult.full),
            });
        }
        return chatView({
            phase: 'error',
            headline: 'One of the steps failed',
            body: shortText(bodyParts.join(' '), 220),
            fullBody: fullBodyParts.join('\n\n'),
            visible: true,
            promote: true,
            dedupeKey: key(evt.tool || '', evt.status || '', evt.exit_code || '', commandText.full || errorResult.full),
        });
    }

    if (t === 'task_done') {
        const failed = taskDoneFailure(evt);
        return chatView({
            phase: failed ? 'error' : 'done',
            headline: failed ? taskDoneLabel(evt) : 'Done',
            visible: true,
            promote: true,
            terminal: true,
            dedupeKey: key(evt.result_status || '', evt.reason_code || ''),
        });
    }

    if (t.includes('error') || t.includes('crash') || t.includes('fail')) {
        const genericError = describeText(evt.error || evt.result_preview || evt.text || '', 220);
        return chatView({
            phase: 'error',
            headline: 'Ran into an issue',
            body: genericError.preview,
            fullBody: genericError.full,
            visible: true,
            promote: true,
            dedupeKey: key(),
        });
    }

    return chatView({ dedupeKey: key() });
}

export function duplicateLogEventKey(evt) {
    const t = evt.type || evt.event || '';
    if (t === 'startup_verification') return `${t}:${evt.git_sha || ''}:${evt.issues_count || 0}`;
    if (t === 'worker_sha_verify') return `${t}:${evt.expected_sha || ''}:${evt.observed_sha || ''}:${evt.ok ? 1 : 0}`;
    if (t === 'deps_sync_ok') return `${t}:${evt.reason || ''}:${evt.source || ''}`;
    return '';
}

export function prettyLogEvent(evt) {
    try {
        return JSON.stringify(evt, null, 2);
    } catch {
        return String(evt);
    }
}

export function getLogTaskGroupId(evt) {
    if (evt.subagent_task_id) return String(evt.subagent_task_id);
    if (evt.task_id) return String(evt.task_id);
    const task = evt.task;
    if (task && typeof task === 'object' && task.id) return String(task.id);
    return '';
}

export function isGroupedTaskEvent(evt) {
    const groupId = getLogTaskGroupId(evt);
    if (!groupId) return false;
    const t = evt.type || evt.event || '';
    return (
        evt.is_progress
        || t.startsWith('task_')
        || t.startsWith('llm_')
        || t.startsWith('tool_')
        || t === 'context_building_started'
        || t === 'context_building_finished'
        || t === 'send_message'
    );
}
