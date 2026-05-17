import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { escapeHtmlAttr, escapeHtmlText as escapeHtml } from './utils.js';
import { apiFetch } from './api_client.js';

function formatFileSize(size) {
    const num = Number(size);
    if (!Number.isFinite(num) || num < 0) return '';
    if (num < 1024) return `${num} B`;
    if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
    return `${(num / (1024 * 1024)).toFixed(1)} MB`;
}

function iconForEntry(entry) {
    return entry.type === 'dir' ? '▸' : '•';
}

function defaultDirectoryMeta() {
    return 'Просматривайте папки, читайте и редактируйте текстовые файлы, загружайте, скачивайте, копируйте и перемещайте файлы. Это файловый менеджер, а не выбор вложений для чата.';
}

function defaultDirectoryContent() {
    return 'Откройте папку или файл на левой панели для просмотра, предпросмотра или редактирования.';
}

export function initFiles({ state: appState, setBeforePageLeave } = {}) {
    const page = document.createElement('div');
    page.id = 'page-files';
    page.className = 'page app-page-glass';
    page.innerHTML = `
        ${renderPageHeader({
            title: 'Файлы',
            icon: PAGE_ICONS.files,
            description: defaultDirectoryMeta(),
            actionsHtml: '<button class="btn btn-default" id="files-refresh">Обновить</button>',
        })}
        <div class="files-layout">
            <section class="files-sidebar">
                <div class="files-toolbar">
                    <input id="files-search" type="text" placeholder="Фильтр текущей папки...">
                </div>
                <div class="files-browser-header">
                    <div id="files-breadcrumb" class="files-breadcrumb"></div>
                    <div class="files-browser-actions">
                        <button class="btn btn-default" id="files-paste" title="Вставить скопированный или перемещённый элемент" hidden>Вставить</button>
                        <button class="btn btn-default" id="files-new-file" title="Создать файл">+ Файл</button>
                        <button class="btn btn-default" id="files-new-dir" title="Создать папку">+ Папка</button>
                    </div>
                </div>
                <div id="files-list" class="files-list scroll-fade-y"></div>
            </section>
            <section class="files-preview">
                <div class="files-preview-header">
                    <div>
                        <div id="files-preview-path" class="files-preview-path">Файлы</div>
                        <div id="files-preview-meta" class="files-preview-meta">${defaultDirectoryMeta()}</div>
                    </div>
                    <div class="files-preview-actions">
                        <button class="btn btn-default" id="files-download" hidden>Скачать</button>
                        <button class="btn btn-default" id="files-open-external" hidden>Открыть снаружи</button>
                        <button class="btn btn-primary" id="files-save" hidden disabled>Сохранить</button>
                    </div>
                </div>
                <div id="files-preview-content" class="files-preview-content scroll-fade-y">${defaultDirectoryContent()}</div>
            </section>
            <div class="files-drop-overlay" aria-hidden="true">
                <div class="files-drop-card">Перетащите файлы для загрузки в текущую папку</div>
            </div>
            <div id="files-context-menu" class="files-context-menu" hidden>
                <button type="button" class="files-context-item" data-action="download">Скачать</button>
                <button type="button" class="files-context-item" data-action="copy">Копировать</button>
                <button type="button" class="files-context-item" data-action="move">Переместить</button>
                <button type="button" class="files-context-item" data-action="paste">Вставить здесь</button>
                <button type="button" class="files-context-item files-context-item-danger" data-action="delete">Удалить</button>
            </div>
            <div id="files-modal" class="files-modal" hidden>
                <div class="files-modal-backdrop" data-close="backdrop"></div>
                <div class="files-modal-card" role="dialog" aria-modal="true" aria-labelledby="files-modal-title">
                    <div class="files-modal-title" id="files-modal-title"></div>
                    <div class="files-modal-message" id="files-modal-message"></div>
                    <input id="files-modal-input" class="files-modal-input" type="text" hidden>
                    <div class="files-modal-actions">
                        <button type="button" class="btn btn-default" id="files-modal-cancel">Отмена</button>
                        <button type="button" class="btn btn-primary" id="files-modal-confirm">OK</button>
                    </div>
                </div>
            </div>
        </div>
    `;
    document.getElementById('content').appendChild(page);

    const layoutEl = page.querySelector('.files-layout');
    const listEl = page.querySelector('#files-list');
    const breadcrumbEl = page.querySelector('#files-breadcrumb');
    const previewPathEl = page.querySelector('#files-preview-path');
    const previewMetaEl = page.querySelector('#files-preview-meta');
    const previewContentEl = page.querySelector('#files-preview-content');
    const contextMenuEl = page.querySelector('#files-context-menu');
    const contextMenuPositionStyle = document.createElement('style');
    contextMenuPositionStyle.id = 'files-context-menu-position-style';
    page.appendChild(contextMenuPositionStyle);
    const modalEl = page.querySelector('#files-modal');
    const modalTitleEl = page.querySelector('#files-modal-title');
    const modalMessageEl = page.querySelector('#files-modal-message');
    const modalInputEl = page.querySelector('#files-modal-input');
    const modalCancelBtn = page.querySelector('#files-modal-cancel');
    const modalConfirmBtn = page.querySelector('#files-modal-confirm');
    const saveBtn = page.querySelector('#files-save');
    const downloadBtn = page.querySelector('#files-download');
    const openExternalBtn = page.querySelector('#files-open-external');
    const pasteBtn = page.querySelector('#files-paste');
    const newFileBtn = page.querySelector('#files-new-file');
    const newDirBtn = page.querySelector('#files-new-dir');
    const searchEl = page.querySelector('#files-search');
    const refreshBtn = page.querySelector('#files-refresh');

    const state = {
        path: '.',
        parentPath: '.',
        entries: [],
        selectedPath: '',
        selectedType: '',
        filter: '',
        rootPath: '',
        dragDepth: 0,
        contextPath: '',
        editorPath: '',
        editorOriginalFilename: '',
        editorOriginal: '',
        editorValue: '',
        editorDirty: false,
        editorWritable: false,
        editorIsNew: false,
        editorFilename: '',
        modalResolve: null,
        clipboard: null,
        contextEntryType: '',
        contextDestinationPath: '.',
    };

    function updateEditorActions() {
        const visible = state.editorWritable && state.selectedType === 'file';
        const canSave = visible && (
            state.editorIsNew
                ? Boolean(state.editorFilename.trim())
                : state.selectedPath === state.editorPath
        ) && (state.editorDirty || state.editorIsNew);
        saveBtn.hidden = !visible;
        saveBtn.disabled = !canSave;
        const fileSelected = state.selectedType === 'file' && Boolean(state.selectedPath);
        downloadBtn.hidden = !fileSelected;
        openExternalBtn.hidden = !fileSelected;
    }

    function resetEditorState() {
        state.editorPath = '';
        state.editorOriginalFilename = '';
        state.editorOriginal = '';
        state.editorValue = '';
        state.editorDirty = false;
        state.editorWritable = false;
        state.editorIsNew = false;
        state.editorFilename = '';
        updateEditorActions();
    }

    function updateClipboardActions() {
        pasteBtn.hidden = !state.clipboard;
        pasteBtn.disabled = !state.clipboard;
        pasteBtn.textContent = state.clipboard
            ? `Вставить (${state.clipboard.mode === 'move' ? 'перемещение' : 'копирование'})`
            : 'Вставить';
    }

    function setPreview({ path, meta, content, html, node }) {
        previewPathEl.textContent = path || 'Выберите файл';
        previewMetaEl.textContent = meta || '';
        if (node) {
            previewContentEl.replaceChildren(node);
            return;
        }
        if (typeof html === 'string') {
            previewContentEl.innerHTML = html;
            return;
        }
        previewContentEl.textContent = content || '';
    }

    function renderEditor(content, options = {}) {
        const wrapper = document.createElement('div');
        wrapper.className = 'files-editor-shell';

        if (options.isNew) {
            const nameInput = document.createElement('input');
            nameInput.className = 'files-editor-name';
            nameInput.type = 'text';
            nameInput.placeholder = 'new-file.txt';
            nameInput.value = state.editorFilename || '';
            nameInput.autocomplete = 'off';
            nameInput.spellcheck = false;
            nameInput.addEventListener('input', () => {
                state.editorFilename = nameInput.value;
                state.editorDirty = state.editorValue !== state.editorOriginal || state.editorFilename !== state.editorOriginalFilename;
                updateEditorActions();
            });
            wrapper.appendChild(nameInput);
        }

        const textarea = document.createElement('textarea');
        textarea.className = 'files-editor';
        textarea.value = content || '';
        textarea.spellcheck = false;
        textarea.placeholder = options.isNew ? 'Start typing file contents...' : '';
        textarea.addEventListener('input', () => {
            state.editorValue = textarea.value;
            state.editorDirty = state.editorValue !== state.editorOriginal || state.editorFilename !== state.editorOriginalFilename;
            updateEditorActions();
        });
        textarea.addEventListener('keydown', (event) => {
            if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
                event.preventDefault();
                saveCurrentFile().catch(showError);
            }
        });
        wrapper.appendChild(textarea);
        return wrapper;
    }

    function closeModal(result) {
        modalEl.hidden = true;
        const resolver = state.modalResolve;
        state.modalResolve = null;
        if (resolver) resolver(result);
    }

    function showModal({ title, message, input = false, initialValue = '', confirmLabel = 'OK', cancelLabel = 'Cancel' }) {
        modalTitleEl.textContent = title || '';
        modalMessageEl.textContent = message || '';
        modalInputEl.hidden = !input;
        modalInputEl.value = input ? initialValue : '';
        modalConfirmBtn.textContent = confirmLabel;
        modalCancelBtn.textContent = cancelLabel;
        modalEl.hidden = false;
        if (input) {
            queueMicrotask(() => modalInputEl.focus());
        } else {
            queueMicrotask(() => modalConfirmBtn.focus());
        }
        return new Promise((resolve) => {
            state.modalResolve = resolve;
        });
    }

    async function canLeaveEditor() {
        if (!state.editorDirty) return true;
        const result = await showModal({
            title: 'Отменить изменения?',
            message: 'В текущем файле есть несохранённые правки. Уйти без сохранения?',
            confirmLabel: 'Отменить',
            cancelLabel: 'Остаться',
        });
        return Boolean(result?.confirmed);
    }

    function showContextMenu(x, y, path, type, destinationPath = '.') {
        state.contextPath = path || '';
        state.contextEntryType = type || '';
        state.contextDestinationPath = destinationPath || '.';
        const downloadItem = contextMenuEl.querySelector('[data-action="download"]');
        const pasteItem = contextMenuEl.querySelector('[data-action="paste"]');
        const deleteItem = contextMenuEl.querySelector('[data-action="delete"]');
        if (downloadItem) {
            downloadItem.hidden = type !== 'file';
        }
        if (pasteItem) {
            pasteItem.hidden = !state.clipboard || (type === 'file');
        }
        if (deleteItem) {
            deleteItem.hidden = !path;
        }
        contextMenuEl.hidden = false;
        const margin = 8;
        const rect = contextMenuEl.getBoundingClientRect();
        const left = Math.min(Math.max(margin, x), Math.max(margin, window.innerWidth - rect.width - margin));
        const top = Math.min(Math.max(margin, y), Math.max(margin, window.innerHeight - rect.height - margin));
        contextMenuPositionStyle.textContent = `#files-context-menu[data-open="1"]{left:${Math.round(left)}px;top:${Math.round(top)}px;}`;
        contextMenuEl.dataset.open = '1';
    }

    function hideContextMenu() {
        state.contextPath = '';
        state.contextEntryType = '';
        state.contextDestinationPath = '.';
        delete contextMenuEl.dataset.open;
        contextMenuPositionStyle.textContent = '';
        contextMenuEl.hidden = true;
    }

    function filteredEntries() {
        const needle = state.filter.trim().toLowerCase();
        if (!needle) return state.entries;
        return state.entries.filter((entry) => entry.name.toLowerCase().includes(needle));
    }

    function renderBreadcrumb(items) {
        breadcrumbEl.innerHTML = '';
        items.forEach((item, idx) => {
            const btn = document.createElement('button');
            btn.className = 'files-crumb';
            btn.textContent = item.name;
            btn.addEventListener('click', () => {
                loadDirectory(item.path).catch(showError);
            });
            breadcrumbEl.appendChild(btn);
            if (idx < items.length - 1) {
                const sep = document.createElement('span');
                sep.className = 'files-crumb-sep';
                sep.textContent = '/';
                breadcrumbEl.appendChild(sep);
            }
        });
    }

    function renderList() {
        listEl.innerHTML = '';
        const entries = filteredEntries();
        const listEntries = [];
        if (state.parentPath && state.path !== '.') {
            listEntries.push({
                name: '..',
                path: state.parentPath,
                type: 'dir',
                isParentLink: true,
            });
        }
        listEntries.push(...entries);

        if (!listEntries.length) {
            const empty = document.createElement('div');
            empty.className = 'files-empty';
            empty.textContent = state.filter ? 'Ничего не найдено в этой папке.' : 'Папка пуста.';
            listEl.appendChild(empty);
            return;
        }

        listEntries.forEach((entry) => {
            const button = document.createElement('button');
            const selected = state.selectedPath === entry.path;
            button.type = 'button';
            button.className = `files-entry ${entry.isParentLink ? 'parent-link' : ''} ${selected ? 'selected' : ''}`;
            button.innerHTML = `
                <span class="files-entry-icon">${iconForEntry(entry)}</span>
                <span class="files-entry-name">${escapeHtml(entry.name)}</span>
                <span class="files-entry-meta">${entry.isParentLink ? 'up' : (entry.type === 'file' ? formatFileSize(entry.size) : 'open')}</span>
            `;
            button.addEventListener('contextmenu', (event) => {
                if (entry.isParentLink) return;
                event.preventDefault();
                state.selectedPath = entry.path;
                state.selectedType = entry.type;
                renderList();
                showContextMenu(
                    event.clientX,
                    event.clientY,
                    entry.path,
                    entry.type,
                    entry.type === 'dir' ? entry.path : state.path || '.',
                );
            });
            button.addEventListener('click', async () => {
                hideContextMenu();
                if (entry.type === 'dir') {
                    state.selectedPath = entry.isParentLink ? '' : entry.path;
                    state.selectedType = 'dir';
                    renderList();
                    loadDirectory(entry.path).catch(showError);
                } else {
                    if (!(await canLeaveEditor())) return;
                    state.selectedPath = entry.path;
                    state.selectedType = entry.type;
                    renderList();
                    loadFile(entry.path, { skipLeaveCheck: true }).catch(showError);
                }
            });
            listEl.appendChild(button);
        });
    }

    function showError(err) {
        setPreview({
            path: 'Файлы',
            meta: 'Ошибка запроса',
            content: err instanceof Error ? err.message : String(err),
        });
    }

    async function loadDirectory(path = '.', options = {}) {
        if (!options.skipEditorReset) {
            if (!options.skipLeaveCheck && !(await canLeaveEditor())) return;
            resetEditorState();
        }
        hideContextMenu();
        const params = new URLSearchParams();
        if (options.useBackendDefault !== true) {
            params.set('path', path);
        }
        const query = params.toString();
        const resp = await apiFetch(`/api/files/list${query ? `?${query}` : ''}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        state.rootPath = data.root_path || state.rootPath;
        state.path = data.path || '.';
        state.parentPath = data.parent_path || '.';
        state.entries = Array.isArray(data.entries) ? data.entries : [];
        if (state.selectedPath && !state.entries.some((entry) => entry.path === state.selectedPath)) {
            state.selectedPath = '';
            state.selectedType = '';
        }
        renderBreadcrumb(Array.isArray(data.breadcrumb) ? data.breadcrumb : []);
        renderList();

        if (!state.selectedPath || state.selectedType === 'dir') {
            setPreview({
                path: data.display_path || state.rootPath || 'Файлы',
                meta: data.truncated ? 'Список каталога сокращён.' : defaultDirectoryMeta(),
                content: defaultDirectoryContent(),
            });
        }
    }

    async function loadFile(path, options = {}) {
        if (!options.skipLeaveCheck && state.selectedPath !== path && !(await canLeaveEditor())) return;
        hideContextMenu();
        const params = new URLSearchParams({ path });
        const resp = await apiFetch(`/api/files/read?${params.toString()}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        if (data.is_image && data.content_url) {
            resetEditorState();
            setPreview({
                path: data.display_path || state.rootPath || 'Файлы',
                meta: `${formatFileSize(data.size)} • ${data.media_type || 'изображение'}`,
                html: `<img class="files-preview-image" src="${escapeHtmlAttr(data.content_url)}" alt="${escapeHtmlAttr(data.name || data.path || 'image')}">`,
            });
            return;
        }

        if (data.is_pdf && data.content_url) {
            resetEditorState();
            const safeUrl = escapeHtmlAttr(data.content_url);
            setPreview({
                path: data.display_path || state.rootPath || 'Файлы',
                meta: `${formatFileSize(data.size)} • предпросмотр PDF`,
                html: `<iframe class="files-preview-frame" sandbox="allow-same-origin" src="${safeUrl}" title="${escapeHtmlAttr(data.name || 'PDF preview')}"></iframe>`,
            });
            return;
        }

        if (!data.is_text) {
            resetEditorState();
            setPreview({
                path: data.display_path || state.rootPath || 'Файлы',
                meta: `${formatFileSize(data.size)} • бинарный или неподдерживаемый формат`,
                content: 'Предпросмотр бинарных и нетекстовых файлов пока не поддерживается.',
            });
            return;
        }

        const editable = !data.truncated;
        state.editorPath = path;
        state.editorOriginalFilename = data.name || '';
        state.editorOriginal = data.content || '';
        state.editorValue = data.content || '';
        state.editorDirty = false;
        state.editorWritable = editable;
        state.editorIsNew = false;
        state.editorFilename = data.name || '';
        updateEditorActions();
        setPreview({
            path: data.display_path || state.rootPath || 'Файлы',
            meta: editable
                ? `${formatFileSize(data.size)} • редактируемый`
                : `${formatFileSize(data.size)} • предпросмотр сокращён • только чтение`,
            node: editable ? renderEditor(data.content || '', { isNew: false }) : document.createTextNode(data.content || ''),
        });
    }

    function filenameFromPath(path) {
        return String(path || '').split('/').filter(Boolean).pop() || 'download';
    }

    async function downloadFile(path, { openExternal = false } = {}) {
        if (!path) return;
        const params = new URLSearchParams({ path });
        const url = `/api/files/download?${params.toString()}`;
        const filename = filenameFromPath(path);
        const bridge = window.pywebview?.api?.download_file_to_downloads;
        if (bridge) {
            const result = await bridge(url, filename, Boolean(openExternal));
            if (!result?.ok) throw new Error(result?.error || 'native download failed');
            setPreview({
                path,
                meta: openExternal ? 'Открыто снаружи' : 'Скачано',
                content: `${filename} saved to ${result.path || 'Downloads'}.`,
            });
            return;
        }
        const resp = await apiFetch(url);
        if (!resp.ok) throw new Error(`download failed: HTTP ${resp.status}`);
        const blob = await resp.blob();
        const blobUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = filename;
        link.rel = 'noopener';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
    }

    async function createDirectory() {
        if (!(await canLeaveEditor())) return;
        const result = await showModal({
            title: 'Создать папку',
            message: 'Введите имя для новой папки в текущем каталоге.',
            input: true,
            confirmLabel: 'Создать',
            cancelLabel: 'Отмена',
        });
        const name = (result?.value || '').trim();
        if (!result?.confirmed || !name) return;
        const resp = await apiFetch('/api/files/mkdir', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path: state.path || '.',
                name,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        state.selectedPath = '';
        state.selectedType = 'dir';
        await loadDirectory(state.path || '.', { skipLeaveCheck: true });
    }

    async function pasteClipboard() {
        if (!state.clipboard) return;
        if (!(await canLeaveEditor())) return;

        const resp = await apiFetch('/api/files/transfer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_path: state.clipboard.path,
                destination_dir: state.path || '.',
                mode: state.clipboard.mode,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        const pastedMode = state.clipboard.mode;
        state.clipboard = null;
        updateClipboardActions();
        state.selectedPath = data.path || '';
        state.selectedType = data.type || '';
        await loadDirectory(state.path || '.', { skipLeaveCheck: true });
        setPreview({
            path: data.display_path || state.rootPath || 'Файлы',
            meta: `${pastedMode === 'move' ? 'Перемещено' : 'Скопировано'}: ${data.type === 'dir' ? 'папка' : 'файл'}`,
            content: '',
        });
    }

    async function pasteClipboardInto(destinationPath) {
        if (!state.clipboard) return;
        if (!(await canLeaveEditor())) return;

        const resp = await apiFetch('/api/files/transfer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_path: state.clipboard.path,
                destination_dir: destinationPath || '.',
                mode: state.clipboard.mode,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        const pastedMode = state.clipboard.mode;
        state.clipboard = null;
        updateClipboardActions();
        const refreshPath = destinationPath || state.path || '.';
        state.selectedPath = data.path || '';
        state.selectedType = data.type || '';
        await loadDirectory(refreshPath, { skipLeaveCheck: true });
        setPreview({
            path: data.display_path || state.rootPath || 'Файлы',
            meta: `${pastedMode === 'move' ? 'Перемещено' : 'Скопировано'}: ${data.type === 'dir' ? 'папка' : 'файл'}`,
            content: '',
        });
    }

    async function deleteSelectedEntry() {
        if (!state.selectedPath) return;
        const entry = state.entries.find((item) => item.path === state.selectedPath);
        if (!entry) return;
        if (!(await canLeaveEditor())) return;

        const result = await showModal({
            title: `Удалить ${entry.type === 'dir' ? 'папку' : 'файл'}?`,
            message: entry.type === 'dir'
                ? `Удалить «${entry.name}» со всем содержимым? Это действие необратимо.`
                : `Удалить «${entry.name}»? Это действие необратимо.`,
            confirmLabel: 'Удалить',
            cancelLabel: 'Отмена',
        });
        if (!result?.confirmed) return;

        const resp = await apiFetch('/api/files/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: state.selectedPath }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        resetEditorState();
        state.selectedPath = '';
        state.selectedType = 'dir';
        await loadDirectory(state.path || '.', { skipLeaveCheck: true, skipEditorReset: true });
        setPreview({
            path: state.rootPath || 'Файлы',
            meta: `${entry.type === 'dir' ? 'Папка' : 'Файл'} удалён`,
            content: '',
        });
    }

    async function uploadFiles(fileList) {
        const files = Array.from(fileList || []);
        if (!files.length) return;

        for (const file of files) {
            const form = new FormData();
            form.set('path', state.path || '.');
            form.set('file', file);

            setPreview({
                path: state.rootPath || 'Файлы',
                meta: `Загрузка в ${state.path || '.'}`,
                content: `Загружается ${file.name}...`,
            });

            const resp = await apiFetch('/api/files/upload', {
                method: 'POST',
                body: form,
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

            state.selectedPath = data.path || '';
            state.selectedType = 'file';
        }

        await loadDirectory(state.path || '.');
        if (state.selectedPath) {
            const selected = state.entries.find((entry) => entry.path === state.selectedPath);
            setPreview({
                path: selected ? `${state.rootPath || 'Файлы'}/${selected.name}` : (state.rootPath || 'Файлы'),
                meta: selected ? `${formatFileSize(selected.size)} • загружено` : 'Загрузка завершена',
                content: '',
            });
        }
    }

    async function saveCurrentFile() {
        if (!state.editorWritable) return;
        const relName = state.editorFilename.trim();
        const savePath = state.editorIsNew
            ? (state.path && state.path !== '.' ? `${state.path}/${relName}` : relName)
            : state.editorPath;
        if (!savePath) return;
        const resp = await apiFetch('/api/files/write', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                path: savePath,
                content: state.editorValue,
                create: state.editorIsNew,
            }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);

        state.selectedPath = data.path || savePath;
        state.selectedType = 'file';
        state.editorPath = data.path || savePath;
        state.editorFilename = data.name || relName;
        state.editorOriginalFilename = state.editorFilename;
        state.editorIsNew = false;
        state.editorOriginal = state.editorValue;
        state.editorDirty = false;
        updateEditorActions();
        setPreview({
            path: data.display_path || state.rootPath || 'Файлы',
            meta: `${formatFileSize(data.size)} • сохранено`,
            node: renderEditor(state.editorValue, { isNew: false }),
        });
        await loadDirectory(state.path || '.', { skipEditorReset: true, skipLeaveCheck: true });
    }

    function createNewFile(options = {}) {
        if (state.editorDirty && !options.force) return;
        hideContextMenu();
        state.selectedPath = '';
        state.selectedType = 'file';
        state.editorPath = '';
        state.editorOriginalFilename = '';
        state.editorOriginal = '';
        state.editorValue = '';
        state.editorDirty = true;
        state.editorWritable = true;
        state.editorIsNew = true;
        state.editorFilename = '';
        renderList();
        updateEditorActions();
        setPreview({
            path: state.path && state.path !== '.'
                ? `${state.rootPath || 'Файлы'}/${state.path}`
                : (state.rootPath || 'Файлы'),
            meta: 'Новый файл • редактируемый',
            node: renderEditor('', { isNew: true }),
        });
    }

    searchEl.addEventListener('input', () => {
        state.filter = searchEl.value || '';
        renderList();
    });

    newFileBtn.addEventListener('click', async () => {
        if (!(await canLeaveEditor())) return;
        createNewFile({ force: true });
    });

    newDirBtn.addEventListener('click', () => {
        createDirectory().catch(showError);
    });

    pasteBtn.addEventListener('click', () => {
        pasteClipboard().catch(showError);
    });

    saveBtn.addEventListener('click', () => {
        saveCurrentFile().catch(showError);
    });
    downloadBtn.addEventListener('click', () => {
        downloadFile(state.selectedPath).catch(showError);
    });
    openExternalBtn.addEventListener('click', () => {
        downloadFile(state.selectedPath, { openExternal: true }).catch(showError);
    });

    layoutEl.addEventListener('dragenter', (event) => {
        event.preventDefault();
        state.dragDepth += 1;
        layoutEl.classList.add('drag-active');
    });

    layoutEl.addEventListener('dragover', (event) => {
        event.preventDefault();
        if (event.dataTransfer) {
            event.dataTransfer.dropEffect = 'copy';
        }
    });

    layoutEl.addEventListener('dragleave', (event) => {
        event.preventDefault();
        state.dragDepth = Math.max(0, state.dragDepth - 1);
        if (state.dragDepth === 0) {
            layoutEl.classList.remove('drag-active');
        }
    });

    layoutEl.addEventListener('drop', async (event) => {
        event.preventDefault();
        state.dragDepth = 0;
        layoutEl.classList.remove('drag-active');
        hideContextMenu();
        try {
            await uploadFiles(event.dataTransfer && event.dataTransfer.files);
        } catch (err) {
            showError(err);
        }
    });

    contextMenuEl.addEventListener('click', (event) => {
        const action = event.target instanceof HTMLElement ? event.target.dataset.action : '';
        if (action === 'download') {
            downloadFile(state.contextPath).catch(showError);
        } else if (action === 'copy' || action === 'move') {
            const entry = state.entries.find((item) => item.path === state.contextPath);
            if (entry) {
                state.clipboard = {
                    mode: action,
                    path: entry.path,
                    name: entry.name,
                    type: entry.type,
                };
                updateClipboardActions();
                setPreview({
                    path: state.rootPath || 'Файлы',
                    meta: `${action === 'move' ? 'Перемещение' : 'Копирование'} готово`,
                    content: `«${entry.name}» будет ${action === 'move' ? 'перемещён' : 'скопирован'} в следующую папку, где вы нажмёте «Вставить».`,
                });
            }
        } else if (action === 'paste') {
            pasteClipboardInto(state.contextDestinationPath).catch(showError);
        } else if (action === 'delete') {
            deleteSelectedEntry().catch(showError);
        }
        hideContextMenu();
    });

    modalCancelBtn.addEventListener('click', () => {
        closeModal({ confirmed: false, value: '' });
    });

    modalConfirmBtn.addEventListener('click', () => {
        closeModal({ confirmed: true, value: modalInputEl.hidden ? '' : modalInputEl.value });
    });

    modalEl.addEventListener('click', (event) => {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (target?.dataset.close === 'backdrop') {
            closeModal({ confirmed: false, value: '' });
        }
    });

    modalInputEl.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            closeModal({ confirmed: true, value: modalInputEl.value });
        }
        if (event.key === 'Escape') {
            event.preventDefault();
            closeModal({ confirmed: false, value: '' });
        }
    });

    document.addEventListener('click', () => {
        hideContextMenu();
    });

    listEl.addEventListener('contextmenu', (event) => {
        if (event.target === listEl && state.clipboard) {
            event.preventDefault();
            showContextMenu(event.clientX, event.clientY, '', 'dir', state.path || '.');
        }
    });

    window.addEventListener('blur', () => {
        hideContextMenu();
    });

    refreshBtn.addEventListener('click', () => {
        loadDirectory(state.path || '.').catch(showError);
    });

    window.addEventListener('beforeunload', (event) => {
        if (!state.editorDirty) return;
        event.preventDefault();
        event.returnValue = '';
    });

    document.addEventListener('keydown', (event) => {
        const active = document.activeElement;
        const inEditor = active && (
            active.classList?.contains('files-editor') ||
            active.classList?.contains('files-editor-name') ||
            active.id === 'files-search' ||
            active.id === 'files-modal-input'
        );
        if (!page.classList.contains('active')) return;
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
            if (!inEditor) return;
            event.preventDefault();
            saveCurrentFile().catch(showError);
            return;
        }
        if (event.key === 'Delete') {
            if (inEditor || modalEl.hidden === false) return;
            event.preventDefault();
            deleteSelectedEntry().catch(showError);
        }
    });

    if (typeof setBeforePageLeave === 'function') {
        setBeforePageLeave(async ({ from }) => {
            if (from !== 'files') return true;
            return canLeaveEditor();
        });
    }
    if (appState) {
        appState.filesState = state;
    }

    updateClipboardActions();
    loadDirectory('.', { useBackendDefault: true }).catch(showError);
}
