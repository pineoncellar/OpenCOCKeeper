/*- encoding: utf-8 -*/
/*
@File     :   sse.js
@Desc     :   SSE 客户端封装 — 自动重连、事件回调、连接状态通知
@Note     :   通过 EventSource 连接 /api/trace/stream，支持 world_id 过滤
*/

class SSEClient {
    constructor(url, options = {}) {
        this.url = url;
        this.onEvent = options.onEvent || (() => {});        // 每条事件回调
        this.onStatus = options.onStatus || (() => {});       // 连接状态回调
        this.onError = options.onError || (() => {});
        this.reconnectDelay = options.reconnectDelay || 2000;
        this._source = null;
        this._shouldReconnect = true;
    }

    connect() {
        this._shouldReconnect = true;
        this._doConnect();
    }

    disconnect() {
        this._shouldReconnect = false;
        if (this._source) {
            this._source.close();
            this._source = null;
        }
        this.onStatus('disconnected');
    }

    _doConnect() {
        if (this._source) {
            this._source.close();
        }
        this._source = new EventSource(this.url);
        this.onStatus('connecting');

        this._source.onopen = () => {
            this.onStatus('connected');
        };

        this._source.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);
                this.onEvent(data);
            } catch (e) {
                console.warn('SSE 解析失败:', e);
            }
        };

        this._source.onerror = () => {
            this.onStatus('disconnected');
            this._source.close();
            this._source = null;
            if (this._shouldReconnect) {
                setTimeout(() => this._doConnect(), this.reconnectDelay);
            }
        };
    }
}