import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { showToast } from './toast.js';
import { apiFetch } from './api_client.js';

export function initUpdates({ mount, state }) {
    const page = document.createElement('div');
    page.id = 'page-updates';
    page.className = 'settings-embedded-content settings-updates-panel';
    // Keep the update action beside the status it refreshes.
    page.innerHTML = `
        <div class="updates-scroll">
            <section class="updates-card" id="updates-status-card">
                <div class="updates-card-head">
                    <div class="updates-card-head-main">
                        <div class="section-title">Официальные обновления</div>
                        <div class="updates-summary" id="updates-summary">Загрузка статуса обновлений...</div>
                    </div>
                    <div class="updates-head-actions">
                        <span class="status-badge offline" id="updates-badge">Ожидание</span>
                        <button class="btn btn-default btn-sm" id="btn-update-check">Проверить обновления</button>
                    </div>
                </div>
                <div class="updates-meta" id="updates-meta"></div>
                <div class="updates-actions">
                    <button class="btn btn-primary" id="btn-update-apply" disabled>Обновить сейчас</button>
                </div>
            </section>
            <section class="updates-card">
                <div class="evo-versions-header">
                    <div id="updates-current" class="evo-versions-branch"></div>
                    <button class="btn btn-primary" id="updates-promote">Сделать стабильной</button>
                </div>
                <div class="evo-versions-cols">
                    <div class="evo-versions-col">
                        <h3 class="section-title">Локальное восстановление: последние коммиты</h3>
                        <div id="updates-commits" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Официальные релизы</h3>
                        <div id="updates-official-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                    <div class="evo-versions-col">
                        <h3 class="section-title">Локальное восстановление: локальные теги</h3>
                        <div id="updates-tags" class="log-scroll evo-versions-list"></div>
                    </div>
                </div>
            </section>
        </div>
    `;
    mount.appendChild(page);

    const checkBtn = page.querySelector('#btn-update-check');
    const applyBtn = page.querySelector('#btn-update-apply');
    const badge = page.querySelector('#updates-badge');
    const summary = page.querySelector('#updates-summary');
    const meta = page.querySelector('#updates-meta');
    const current = page.querySelector('#updates-current');
    const commitsDiv = page.querySelector('#updates-commits');
    const officialTagsDiv = page.querySelector('#updates-official-tags');
    const tagsDiv = page.querySelector('#updates-tags');
    let latestStatus = null;

    function setBadge(kind, text) {
        badge.className = `status-badge ${kind}`;
        badge.textContent = text;
    }

    function divergenceText(data) {
        const parts = [];
        if (data.behind) parts.push(`${data.behind} incoming`);
        if (data.ahead) parts.push(`${data.ahead} local`);
        if (data.dirty_count) parts.push(`${data.dirty_count} dirty`);
        return parts.join(' / ') || 'clean';
    }

    function renderStatus(data) {
        latestStatus = data;
        const unmanaged = data.managed === false
            || (Array.isArray(data.warnings) && data.warnings.includes('managed_updates_unavailable'));
        if (unmanaged) {
            summary.textContent = 'Управляемые обновления недоступны для этого репозитория.';
            meta.innerHTML = `
                <span class="evo-runtime-chip"><strong>Режим:</strong> исходники</span>
                <span class="evo-runtime-chip"><strong>Действие:</strong> используйте git или установите управляемую сборку</span>
            `;
            applyBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Недоступно';
            setBadge('offline', 'Недоступно');
            return;
        }
        if (Array.isArray(data.warnings) && data.warnings.includes('official_status_requires_check')) {
            summary.textContent = 'Нажмите «Проверить обновления», чтобы обновить статус официальных обновлений.';
            meta.innerHTML = '<span class="evo-runtime-chip"><strong>Официальный репозиторий:</strong> joi-lab/ouroboros-desktop</span>';
            applyBtn.disabled = true;
            applyBtn.dataset.safe = '0';
            applyBtn.textContent = 'Требуется проверка';
            setBadge('offline', 'Не проверялось');
            return;
        }
        const currentVersion = data.current_version || 'unknown';
        const latestVersion = data.latest_version || 'unknown';
        const currentSha = data.current_short_sha || '?';
        const latestSha = data.latest_short_sha || '?';
        const latestMsg = data.latest_message || 'No remote message.';
        const canUpdate = Boolean(data.available);
        const safe = Boolean(data.safe_to_apply);
        summary.textContent = canUpdate
            ? `Доступно обновление: ${currentVersion} (${currentSha}) -> ${latestVersion} (${latestSha})`
            : `Ouroboros актуален: ${currentVersion} (${currentSha}).`;
        meta.innerHTML = [
            `<span class="evo-runtime-chip"><strong>Официальный репозиторий:</strong> joi-lab/ouroboros-desktop</span>`,
            `<span class="evo-runtime-chip"><strong>Ветка:</strong> ${escapeHtml(data.remote || 'managed')}/${escapeHtml(data.remote_branch || '')}</span>`,
            `<span class="evo-runtime-chip"><strong>Расхождение:</strong> ${escapeHtml(divergenceText(data))}</span>`,
            `<span class="evo-runtime-chip"><strong>Последнее:</strong> ${escapeHtml(latestMsg)}</span>`,
        ].join('');
        applyBtn.disabled = !canUpdate;
        applyBtn.dataset.safe = safe ? '1' : '0';
        applyBtn.textContent = !canUpdate ? 'Нет обновлений' : (safe ? 'Обновить сейчас' : 'Обновить с параметрами');
        setBadge(canUpdate ? (safe ? 'online' : 'starting') : 'offline', canUpdate ? 'Доступно' : 'Актуально');
    }

    async function loadStatus({ fetchRemote = false } = {}) {
        checkBtn.disabled = true;
        setBadge('starting', fetchRemote ? 'Проверка...' : 'Загрузка...');
        try {
            const resp = await apiFetch(fetchRemote ? '/api/update/check' : '/api/update/status', {
                method: fetchRemote ? 'POST' : 'GET',
                cache: 'no-store',
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            renderStatus(data);
            renderOfficialTags(data.official_tags || []);
        } catch (err) {
            summary.textContent = `Не удалось загрузить статус обновлений: ${err.message || err}`;
            meta.innerHTML = '';
            applyBtn.disabled = true;
            setBadge('error', 'Ошибка');
        } finally {
            checkBtn.disabled = false;
        }
    }

    function renderVersionRow(item, labelText, targetId) {
        const row = document.createElement('div');
        row.className = 'log-entry evo-versions-row';
        const date = (item.date || '').slice(0, 16).replace('T', ' ');
        const msg = escapeHtml((item.message || '').slice(0, 72));
        row.innerHTML = `
            <span class="log-type tools evo-versions-row-label">${escapeHtml(labelText)}</span>
            <span class="log-ts">${escapeHtml(date)}</span>
            <span class="log-msg evo-versions-row-msg">${msg}</span>
            <button class="btn btn-danger btn-xs" data-target="${escapeHtml(targetId)}">Восстановить</button>
        `;
        row.querySelector('button').addEventListener('click', () => rollback(targetId));
        return row;
    }

    function renderOfficialTags(tags) {
        officialTagsDiv.innerHTML = '';
        (tags || []).forEach((tag) => {
            const row = document.createElement('div');
            row.className = 'log-entry evo-versions-row';
            row.innerHTML = `
                <span class="log-type tools evo-versions-row-label">${escapeHtml(tag.tag || '')}</span>
                <span class="log-msg evo-versions-row-msg">${escapeHtml((tag.sha || '').slice(0, 12))}</span>
            `;
            officialTagsDiv.appendChild(row);
        });
        if (!tags?.length) officialTagsDiv.innerHTML = '<div class="evo-empty">Нажмите «Проверить обновления» для загрузки официальных релизов.</div>';
    }

    async function loadVersions() {
        try {
            const resp = await apiFetch('/api/git/log', { cache: 'no-store' });
            if (!resp.ok) throw new Error('Git log API error ' + resp.status);
            const data = await resp.json();
            current.textContent = `Branch: ${data.branch || '?'} @ ${data.sha || '?'}`;
            commitsDiv.innerHTML = '';
            (data.commits || []).forEach((commit) => {
                commitsDiv.appendChild(renderVersionRow(commit, commit.short_sha || commit.sha?.slice(0, 8), commit.sha));
            });
            if (!data.commits?.length) commitsDiv.innerHTML = '<div class="evo-empty">Коммиты не найдены</div>';
            tagsDiv.innerHTML = '';
            (data.tags || []).forEach((tag) => {
                tagsDiv.appendChild(renderVersionRow(tag, tag.tag, tag.tag));
            });
            if (!data.tags?.length) tagsDiv.innerHTML = '<div class="evo-empty">Теги не найдены</div>';
        } catch (err) {
            const msg = `<div class="evo-empty evo-empty-error">Failed to load: ${escapeHtml(err.message || err)}</div>`;
            commitsDiv.innerHTML = msg;
            tagsDiv.innerHTML = msg;
            current.textContent = 'Ветка: неизвестна';
        }
    }

    async function rollback(target) {
        if (!confirm(`Откатиться к ${target}?\n\nБудет сохранён снимок текущего состояния. Сервер перезапустится.`)) return;
        try {
            const resp = await apiFetch('/api/git/rollback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target }),
            });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(`Откат выполнен: ${data.message}. Сервер перезапускается...`, 'success');
            } else {
                showToast(`Откат не удался: ${data.error || 'неизвестная ошибка'}`, 'error');
            }
        } catch (err) {
            showToast('Откат не удался: ' + (err.message || err), 'error');
        }
    }

    async function applyUpdate() {
        if (!latestStatus?.available) return;
        const safe = latestStatus.safe_to_apply;
        let strategy = 'replace';
        if (!safe) {
            const localBits = divergenceText(latestStatus);
            const proceed = confirm(
                `Это обновление заменит активный управляемый репозиторий выбранной официальной версией.\n\nЛокальное состояние: ${localBits}\n\nЛокальные коммиты будут сохранены в ветке local-keep-*. Изменённые файлы сохранятся в снимке. Продолжить?`,
            );
            if (!proceed) return;
            strategy = latestStatus.ahead ? 'stash' : 'replace';
        }
        applyBtn.disabled = true;
        applyBtn.textContent = 'Подготовка...';
        try {
            const resp = await apiFetch('/api/update/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ strategy }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
            const keep = data.keep_branch ? ` Локальные коммиты сохранены в ${data.keep_branch}.` : '';
            showToast(`Обновление подготовлено. Сервер перезапускается.${keep}`, 'success');
        } catch (err) {
            showToast('Обновление не удалось: ' + (err.message || err), 'error');
            applyBtn.disabled = false;
            applyBtn.textContent = safe ? 'Обновить сейчас' : 'Обновить с параметрами';
        }
    }

    checkBtn.addEventListener('click', () => {
        loadStatus({ fetchRemote: true });
        loadVersions();
    });
    applyBtn.addEventListener('click', applyUpdate);
    page.querySelector('#updates-promote').addEventListener('click', async () => {
        if (!confirm('Перевести текущую ветку ouroboros в ouroboros-stable?')) return;
        try {
            const resp = await apiFetch('/api/git/promote', { method: 'POST' });
            const data = await resp.json();
            if (data.status === 'ok') {
                showToast(data.message, 'success');
            } else {
                showToast('Error: ' + (data.error || 'unknown'), 'error');
            }
        } catch (err) {
            showToast('Failed: ' + (err.message || err), 'error');
        }
    });

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab !== 'updates' || state.activePage !== 'dashboard') return;
        loadStatus({ fetchRemote: false });
        loadVersions();
    });
}
