import { apiFetch } from './api_client.js';
/** WebSocket manager; connect after modules register listeners. */

export class WS {
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.listeners = {};
        this.reconnectDelay = 1000;
        this.maxDelay = 10000;
        this._wasConnected = false;
        this._lastSha = null;
        this._lastMessageAt = 0;
        this._reconnectTimer = null;
        this._uiRecoveryTimer = null;
        this._watchdogTimer = null;
        this._pendingMessages = [];
        this._nextClientMessageId = 1;
    }

    _getUrl() {
        return typeof this.url === 'function' ? this.url() : this.url;
    }

    _clearReconnectTimer() {
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
    }

    _clearUiRecoveryTimer() {
        if (this._uiRecoveryTimer) {
            clearTimeout(this._uiRecoveryTimer);
            this._uiRecoveryTimer = null;
        }
    }

    _clearWatchdogTimer() {
        if (this._watchdogTimer) {
            clearInterval(this._watchdogTimer);
            this._watchdogTimer = null;
        }
    }

    _freshWindowUrl(reason = '') {
        const url = new URL(window.location.href);
        url.searchParams.set('_ouro_refresh', String(Date.now()));
        if (reason) url.searchParams.set('_ouro_reason', reason);
        return url.toString();
    }

    _refreshWindow(reason = '') {
        window.location.replace(this._freshWindowUrl(reason));
    }

    _scheduleUiRecovery(reason, delay = 15000) {
        if (this._uiRecoveryTimer) return;
        this._uiRecoveryTimer = setTimeout(async () => {
            this._uiRecoveryTimer = null;
            try {
                const resp = await apiFetch('/api/state', { cache: 'no-store' });
                if (resp.ok) {
                    this._refreshWindow(reason);
                    return;
                }
            } catch {}
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                this._scheduleUiRecovery(reason, Math.min(Math.round(delay * 1.5), 30000));
            }
        }, delay);
    }

    _startWatchdog(socket) {
        this._clearWatchdogTimer();
        this._watchdogTimer = setInterval(() => {
            if (this.ws !== socket || socket.readyState !== WebSocket.OPEN) return;
            if (Date.now() - this._lastMessageAt < 45000) return;
            console.warn('WebSocket watchdog forcing reconnect after stale inbound stream');
            try { socket.close(); } catch {}
        }, 10000);
    }

    _scheduleReconnect() {
        if (this._reconnectTimer) return;
        document.getElementById('reconnect-overlay')?.classList.add('visible');
        this._scheduleUiRecovery('socket-disconnect', 15000);
        const delay = this.reconnectDelay;
        this._reconnectTimer = setTimeout(() => {
            this._reconnectTimer = null;
            this.connect();
        }, delay);
        this.reconnectDelay = Math.min(Math.round(this.reconnectDelay * 1.5), this.maxDelay);
    }

    _refreshStateAfterOpen(previouslyConnected) {
        apiFetch('/api/state', { cache: 'no-store' }).then(r => r.json()).then(d => {
            const newSha = d.sha || '';
            if (previouslyConnected && newSha) {
                if (!this._lastSha || this._lastSha !== newSha) {
                    // Reload on SHA change so PyWebView picks up new JS/CSS after restart.
                    this._refreshWindow('sha-change');
                    return;
                }
            }
            this._lastSha = newSha || this._lastSha;
        }).catch(() => {});
    }

    _flushPendingMessages() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this._pendingMessages.length === 0) {
            return;
        }
        const queued = [...this._pendingMessages];
        this._pendingMessages = [];
        for (const msg of queued) {
            try {
                this.ws.send(JSON.stringify(msg));
                this.emit('outbound_sent', {
                    clientMessageId: msg.client_message_id || '',
                    queued: true,
                    type: msg.type || '',
                });
            } catch {
                this._pendingMessages.unshift(msg);
                this._scheduleReconnect();
                break;
            }
        }
    }

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }
        const socket = new WebSocket(this._getUrl());
        this.ws = socket;
        const previouslyConnected = this._wasConnected;
        let disconnected = false;

        const handleDisconnect = () => {
            if (disconnected) return;
            disconnected = true;
            if (this.ws === socket) this.ws = null;
            this._clearWatchdogTimer();
            this.emit('close');
            this._scheduleReconnect();
        };

        socket.onopen = () => {
            if (this.ws !== socket) return;
            this._wasConnected = true;
            this._lastMessageAt = Date.now();
            this._clearReconnectTimer();
            this._clearUiRecoveryTimer();
            this.reconnectDelay = 1000;
            this._startWatchdog(socket);
            this.emit('open');
            document.getElementById('reconnect-overlay')?.classList.remove('visible');
            this._refreshStateAfterOpen(previouslyConnected);
            this._flushPendingMessages();
        };

        socket.onerror = () => {
            handleDisconnect();
            try { socket.close(); } catch {}
        };

        socket.onclose = () => {
            handleDisconnect();
        };

        socket.onmessage = (e) => {
            this._lastMessageAt = Date.now();
            try {
                const msg = JSON.parse(e.data);
                this.emit('message', msg);
                if (msg.type) this.emit(msg.type, msg);
            } catch (err) {
                console.error('WebSocket message handling failed:', err);
            }
        };
    }

    send(msg) {
        const payload = { ...msg };
        if (!payload.client_message_id && payload.type === 'chat') {
            payload.client_message_id = `msg-${Date.now()}-${this._nextClientMessageId++}`;
        }
        if (this.ws?.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(JSON.stringify(payload));
                this.emit('outbound_sent', {
                    clientMessageId: payload.client_message_id || '',
                    queued: false,
                    type: payload.type || '',
                });
                return { status: 'sent', clientMessageId: payload.client_message_id || '' };
            } catch {}
        }
        if (this._pendingMessages.length >= 100) this._pendingMessages.shift();
        this._pendingMessages.push(payload);
        this.emit('outbound_queued', {
            clientMessageId: payload.client_message_id || '',
            type: payload.type || '',
        });
        this._scheduleReconnect();
        this.connect();
        return { status: 'queued', clientMessageId: payload.client_message_id || '' };
    }

    on(event, fn) {
        (this.listeners[event] ||= []).push(fn);
    }

    emit(event, data) {
        (this.listeners[event] || []).forEach(fn => fn(data));
    }
}

export function createWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return new WS(() => `${proto}//${location.host}/ws`);
}
