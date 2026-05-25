import {
    escapeHtmlAttr as escapeHtml,
    grantReady,
    isRateLimitError,
    reviewReady,
    safeExternalHrefAttr as safeExternalUrl,
} from './utils.js';
import { formatRelativeAge, installedTime, renderToneBadge } from './ui_helpers.js';

function hasSkillUiTab(skill, live = {}) {
    return (live?.ui_tabs || []).some((tab) => (tab?.skill || tab?.skill_name || tab?.extension || '') === skill.name);
}

function statusBadge(status, gate = null) {
    const executable = gate && typeof gate.executable_review === 'boolean'
        ? gate.executable_review
        : ['clean', 'warnings'].includes(status);
    const tone = status === 'blockers' ? 'danger' : executable ? 'ok' : status === 'warnings' ? 'warn' : 'muted';
    return renderToneBadge(status || 'pending', tone);
}

function missingGrantLoadError(skill) {
    return !grantReady(skill) && String(skill.load_error || '').includes('missing owner grants');
}

function repairReady(skill) {
    const source = (skill.source || 'native').toLowerCase();
    const payloadRoot = String(skill.payload_root || '');
    return ['clawhub', 'ouroboroshub', 'external'].includes(source)
        && /^skills\/(external|clawhub|ouroboroshub)\//.test(payloadRoot)
        && (skill.review_status === 'blockers' || (Boolean(skill.load_error) && !missingGrantLoadError(skill)));
}

function primaryAction(skill, reviewInProgress, repairInProgress, live) {
    if (reviewInProgress) return { label: 'Reviewing...', disabled: true };
    if (repairInProgress) return { label: 'Repairing...', disabled: true };
    if (skill.lifecycle_virtual && (skill.load_error || isRateLimitError(skill.load_error)) && (skill.source || '').toLowerCase() === 'clawhub') {
        return { action: 'retry_install', label: isRateLimitError(skill.load_error) ? 'Retry later' : 'Retry install' };
    }
    if ((skill.load_error && !missingGrantLoadError(skill)) || (skill.review_status === 'blockers' && !reviewReady(skill))) {
        return repairReady(skill) ? { action: 'repair', label: 'Repair' } : { label: '', disabled: true };
    }
    if (!reviewReady(skill)) return { action: skill.review_stale ? 'rereview' : 'review', label: skill.review_stale ? 'Re-review' : 'Review' };
    const grants = skill.grants || {};
    const missing = [
        ...(grants.missing_keys || grants.requested_keys || []),
        ...(grants.missing_permissions || grants.requested_permissions || []),
    ];
    if (skill.is_self_authored && !skill.enabled) return { action: 'approve_enable', label: 'Approve & enable', keys: missing.join(',') };
    if (!grantReady(skill)) return { action: 'grant', label: 'Grant access', keys: missing.join(',') };
    if (skill.enabled && skill.type === 'extension' && skill.live_loaded && hasSkillUiTab(skill, live)) {
        return { action: 'open_widgets', label: 'Open widgets' };
    }
    return { label: '' };
}

function statusChip(skill, action, live) {
    let status = { tone: 'muted', label: 'Off' };
    if (!grantReady(skill)) status = { tone: 'warn', label: 'Needs access grant' };
    else if (skill.lifecycle_virtual && isRateLimitError(skill.load_error)) status = { tone: 'warn', label: 'Rate limited' };
    else if (skill.load_error) status = { tone: 'danger', label: 'Failed to load' };
    else if (!reviewReady(skill)) status = { tone: 'warn', label: 'Needs review' };
    else if (skill.enabled && skill.type === 'extension') {
        status = skill.live_loaded && (skill.dispatch_live || hasSkillUiTab(skill, live))
            ? { tone: 'ok', label: 'Active' }
            : { tone: 'warn', label: skill.live_loaded ? 'Loaded — UI tab pending' : 'Enabled — not loaded' };
    } else if (skill.enabled) status = { tone: 'ok', label: 'Enabled' };
    const attrs = action.action ? `data-skill="${escapeHtml(skill.name)}" data-skill-action="${escapeHtml(action.action)}" role="button" tabindex="0"` : '';
    return `<span class="skills-status-chip skills-status-${status.tone} ${action.action ? 'is-clickable' : ''}" ${attrs}>${escapeHtml(status.label)}</span>`;
}

function sourceChip(skill) {
    const source = (skill.source || 'native').toLowerCase();
    const map = {
        clawhub: ['ClawHub', 'warn'],
        ouroboroshub: ['OuroborosHub', 'ok'],
        self_authored: ['Authored', 'ok'],
        external: ['External', 'muted'],
        user_repo: ['User repo', 'muted'],
    };
    if (!map[source]) return '';
    const [label, tone] = map[source];
    return `<span class="skills-source-chip skills-source-${tone}">${escapeHtml(label)}</span>`;
}

function reviewFindings(skill) {
    const findings = Array.isArray(skill.review_findings) ? skill.review_findings : [];
    if (!findings.length) return '';
    const rows = findings.map((f) => `<li><strong>${escapeHtml(f.verdict || f.severity || '')}</strong> ${escapeHtml(f.item || f.check || f.title || 'finding')}: ${escapeHtml(f.reason || f.message || JSON.stringify(f))}</li>`).join('');
    return `<details class="skills-review-findings"><summary class="muted">${findings.length} review finding${findings.length === 1 ? '' : 's'}</summary><ul>${rows}</ul></details>`;
}

function grantBlock(skill) {
    const grants = skill.grants || {};
    const requested = [...(grants.requested_keys || []), ...(grants.requested_permissions || [])];
    if (!requested.length) return '';
    const missing = [...(grants.missing_keys || []), ...(grants.missing_permissions || [])];
    const granted = [...(grants.granted_keys || []), ...(grants.granted_permissions || [])];
    const tone = grants.unsupported_for_skill_type ? 'muted' : missing.length ? 'warn' : 'ok';
    const status = grants.unsupported_for_skill_type ? 'This skill type cannot receive keys or host permissions.' : missing.length ? 'This skill needs your permission to use the keys and permissions above.' : 'Access granted.';
    return `<div class="skills-access skills-access-${tone}">
        <div class="skills-access-row"><span class="skills-access-label">Needs access</span> ${requested.map((k) => `<code>${escapeHtml(k)}</code>`).join(' ')}</div>
        ${granted.length ? `<div class="skills-access-row"><span class="skills-access-label">Granted</span> ${granted.map((k) => `<code>${escapeHtml(k)}</code>`).join(' ')}</div>` : ''}
        <div class="skills-access-status">${escapeHtml(status)}</div>
    </div>`;
}

function provenanceBlock(prov) {
    if (!prov || typeof prov !== 'object') return '';
    const rows = [];
    if (prov.slug) rows.push(`<span>slug: <code>${escapeHtml(prov.slug)}</code></span>`);
    if (prov.sha256) rows.push(`<span>sha256: <code>${escapeHtml(String(prov.sha256).slice(0, 12))}…</code></span>`);
    if (prov.license) rows.push(`<span>license: ${escapeHtml(prov.license)}</span>`);
    const href = safeExternalUrl(prov.homepage);
    if (href) rows.push(`<a href="${href}" target="_blank" rel="noopener noreferrer">homepage</a>`);
    const warnings = (prov.adapter_warnings || []).map((msg) => `<li>${escapeHtml(msg)}</li>`).join('');
    return (rows.length ? `<div class="skills-card-provenance muted">${rows.join(' · ')}</div>` : '')
        + (warnings ? `<details class="skills-card-warnings"><summary class="muted">adapter warnings</summary><ul>${warnings}</ul></details>` : '');
}

export function renderInstalledSkillCard(skill, reviewingSkills = new Set(), repairingSkills = new Set(), live = {}, options = {}) {
    const safeName = escapeHtml(skill.name);
    const reviewInProgress = reviewingSkills.has(skill.name);
    const repairInProgress = repairingSkills.has(skill.name);
    const action = primaryAction(skill, reviewInProgress, repairInProgress, live);
    const actionAttrs = action.action ? `data-skill="${safeName}" data-skill-action="${escapeHtml(action.action)}" role="button" tabindex="0"` : '';
    const lockReason = !skill.enabled && ((skill.review_gate?.executable_review === false && (skill.review_gate.summary || skill.review_gate.blocking_reason)) || (skill.review_stale ? 'review is stale — re-review the skill first' : ''));
    const source = (skill.source || 'native').toLowerCase();
    const market = source === 'clawhub' || source === 'ouroboroshub';
    const payloadRoot = skill.payload_root || '';
    const localDelete = (source === 'self_authored' || source === 'external') && payloadRoot.startsWith('skills/external/');
    const prov = market ? skill.provenance : null;
    const submit = submitHubReady(skill, Boolean(options.githubTokenConfigured));
    const menu = (market || localDelete || !reviewInProgress || submit.visible)
        ? `<div class="skills-card-menu"><button type="button" class="skills-card-menu-trigger" aria-label="More actions" aria-haspopup="menu" aria-expanded="false" data-skill-menu-trigger>⋮</button><dialog class="skills-card-menu-dialog" role="menu">
            ${!reviewInProgress ? `<button type="button" role="menuitem" class="skills-menu-item skills-review" data-skill="${safeName}">${skill.review_status === 'pending' ? 'Review' : (skill.review_stale ? 'Re-review' : 'Review again')}</button>` : ''}
            ${submit.visible ? `<button type="button" role="menuitem" class="skills-menu-item skills-submit-hub ${submit.disabled ? 'is-disabled' : ''}" data-skill="${safeName}" title="${escapeHtml(submit.reason)}" data-submit-disabled="${submit.disabled ? 'true' : 'false'}" data-submit-reason="${escapeHtml(submit.reason)}" aria-disabled="${submit.disabled ? 'true' : 'false'}">Submit to OuroborosHub</button>` : ''}
            ${market ? `<button type="button" role="menuitem" class="skills-menu-item skills-update" data-skill="${safeName}" data-source="${escapeHtml(source)}">Update</button><button type="button" role="menuitem" class="skills-menu-item skills-uninstall" data-skill="${safeName}" data-source="${escapeHtml(source)}">Uninstall</button>` : ''}
            ${localDelete ? `<button type="button" role="menuitem" class="skills-menu-item skills-delete-local" data-skill="${safeName}" data-payload-root="${escapeHtml(payloadRoot)}">Delete</button>` : ''}
        </dialog></div>` : '';
    const primary = action.action ? `<button type="button" class="btn btn-primary skills-primary-action" data-skill="${safeName}" data-skill-action="${escapeHtml(action.action)}" ${action.keys ? `data-keys="${escapeHtml(action.keys)}"` : ''} ${action.disabled ? 'disabled' : ''}>${escapeHtml(action.label)}</button>` : '';
    const toggle = skill.lifecycle_virtual ? '' : `<label class="skills-switch ${lockReason ? 'is-locked' : ''}" ${lockReason && action.action ? actionAttrs : ''} title="${escapeHtml(lockReason ? `Locked: ${lockReason}` : (skill.enabled ? 'Turn skill off' : 'Turn skill on'))}">
        <input type="checkbox" class="skills-toggle" role="switch" data-skill="${safeName}" ${skill.enabled ? 'checked' : ''} ${lockReason ? 'disabled' : ''} aria-checked="${skill.enabled ? 'true' : 'false'}" aria-label="${escapeHtml(lockReason ? `${skill.name} (locked: ${lockReason})` : skill.name)}">
        <span class="skills-switch-track" aria-hidden="true"><span class="skills-switch-thumb"></span></span>
    </label>`;
    const details = `<details class="skills-details"><summary>Show details</summary>
        <div class="skills-detail-row"><span class="skills-detail-label">Type</span><code>${escapeHtml(skill.type || 'skill')}</code> · version ${escapeHtml(skill.version || '—')} · source ${escapeHtml(source)}</div>
        <div class="skills-detail-row"><span class="skills-detail-label">Review</span>${statusBadge(skill.review_status, skill.review_gate)}${skill.review_stale ? ' <span class="skills-badge skills-badge-warn">stale</span>' : ''}</div>
        <div class="skills-detail-row"><span class="skills-detail-label">Permissions</span>${(skill.permissions || []).map((p) => `<code>${escapeHtml(p)}</code>`).join(' ') || '<i class="muted">none</i>'}</div>
        ${provenanceBlock(prov)}
    </details>`;
    return `<article class="skills-card" data-skill="${safeName}" ${reviewInProgress ? 'data-reviewing="1"' : ''} ${repairInProgress ? 'data-repairing="1"' : ''}>
        <header class="skills-card-head">
            <div class="skills-card-title"><h3>${safeName}${sourceChip(skill) ? ` ${sourceChip(skill)}` : ''}</h3>${skill.description ? `<p class="skills-card-desc">${escapeHtml(skill.description)}</p>` : ''}${formatRelativeAge(installedTime(skill)) ? `<div class="skills-card-installed muted">${escapeHtml(formatRelativeAge(installedTime(skill)))}</div>` : ''}</div>
            <div class="skills-card-toggle">${statusChip(skill, action, live)}${primary}${toggle}${menu}</div>
        </header>
        ${lockReason ? `<div class="skills-lock-hint ${action.action ? 'is-clickable' : ''}" title="${escapeHtml(lockReason)}" ${actionAttrs}>Locked: ${escapeHtml(lockReason)}</div>` : ''}
        ${reviewInProgress ? '<div class="skills-review-progress" role="status" aria-live="polite"><span class="skills-review-spinner" aria-hidden="true"></span><span>Review in progress</span></div>' : ''}
        ${repairInProgress ? '<div class="skills-review-progress skills-repair-progress" role="status" aria-live="polite"><span class="skills-review-spinner" aria-hidden="true"></span><span>Repair task is being queued</span></div>' : ''}
        ${grantBlock(skill)}
        ${reviewFindings(skill)}
        ${skill.load_error && !missingGrantLoadError(skill) ? `<div class="skills-load-error">${escapeHtml(skill.load_error)}</div>` : ''}
        <footer class="skills-card-actions">${details}</footer>
    </article>`;
}

function submitHubReady(skill, githubTokenConfigured = false) {
    const source = (skill.source || 'native').toLowerCase();
    const visible = ['external', 'self_authored', 'user_repo', 'ouroboroshub', 'clawhub'].includes(source);
    if (!visible) return { visible: false, disabled: true, reason: '' };
    if (!githubTokenConfigured) return { visible: true, disabled: true, reason: 'Configure GITHUB_TOKEN in Settings -> Secrets' };
    if (skill.review_status !== 'clean' || skill.review_stale) return { visible: true, disabled: true, reason: 'Skill needs a fresh clean review before submission' };
    return { visible: true, disabled: false, reason: 'Open a PR to OuroborosHub from your GitHub fork' };
}
