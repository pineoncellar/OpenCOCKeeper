/*- encoding: utf-8 -*/
/*
@File     :   ws.js
@Desc     :   WebSocket 客户端封装 — 自动重连、事件回调、发送 JSON 帧
@Note     :   连接 /ws/game；指数退避重连；onFrame 接收所有出站帧，
             onStatus 通知连接状态变化
*/

class WSClient {
    constructor(url, options = {}) {
        this.url = url;
        this.onFrame = options.onFrame || (() => {});   // 每条出站帧回调
        this.onStatus = options.onStatus || (() => {});  // 连接状态回调
        this.reconnectDelay = options.reconnectDelay || 1500;
        this.maxReconnect = options.maxReconnect || 30000;
        this._ws = null;
        this._shouldReconnect = true;
        this._retryMs = this.reconnectDelay;
        this._sendQueue = [];
    }

    connect() {
        this._shouldReconnect = true;
        this._open();
    }

    disconnect() {
        this._shouldReconnect = false;
        if (this._ws) {
            this._ws.close();
            this._ws = null;
        }
        this.onStatus('disconnected');
    }

    send(frame) {
        // 状态：连接未就绪时入队，连接建立后补发
        if (this._ws && this._ws.readyState === WebSocket.OPEN) {
            this._ws.send(JSON.stringify(frame));
        } else {
            this._sendQueue.push(frame);
        }
    }

    _open() {
        try {
            this._ws = new WebSocket(this.url);
        } catch (e) {
            this.onStatus('disconnected');
            return;
        }
        this.onStatus('connecting');

        this._ws.onopen = () => {
            this._retryMs = this.reconnectDelay;  // 状态：连接成功重置退避
            this.onStatus('connected');
            // 状态：补发连接期间排队的帧
            while (this._sendQueue.length > 0) {
                const f = this._sendQueue.shift();
                this._ws.send(JSON.stringify(f));
            }
        };

        this._ws.onmessage = (ev) => {
            try {
                this.onFrame(JSON.parse(ev.data));
            } catch (e) {
                console.warn('WS 帧解析失败:', e);
            }
        };

        this._ws.onclose = () => {
            this._ws = null;
            this.onStatus('disconnected');
            if (this._shouldReconnect) {
                setTimeout(() => this._open(), this._retryMs);
                this._retryMs = Math.min(this._retryMs * 2, this.maxReconnect);  // 状态：指数退避
            }
        };

        this._ws.onerror = () => {
            // 状态：错误由 onclose 统一处理重连，这里仅记录
            console.warn('WS 错误');
        };
    }
}