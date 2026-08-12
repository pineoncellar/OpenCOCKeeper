/*- encoding: utf-8 -*/
/*
@File     :   app.js
@Desc     :   WebUI 核心状态管理 — Tab 路由 + SSE 连接 + 事件分派
@Note     :   全局单例 App 对象，各组件通过 App 访问共享状态
*/

const App = {
    // ====================================================================
    // 初始化
    // ====================================================================

    async init() {
        this.traceViewer = new TraceViewer();
        this._connStatus = document.getElementById('conn-status');
        this._statusTime = document.getElementById('status-time');

        this._initTabs();
        this._initSSE();
        this._startClock();
    },

    // ====================================================================
    // Tab 路由
    // ====================================================================

    _initTabs() {
        const nav = document.getElementById('tab-nav');
        nav.addEventListener('click', (e) => {
            const btn = e.target.closest('.tab-btn');
            if (!btn || btn.disabled) return;

            // 状态：切换 Tab 激活状态
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // 状态：切换面板可见性
            const tab = btn.dataset.tab;
            document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
            const panel = document.getElementById('panel-' + tab);
            if (panel) panel.classList.add('active');
        });
    },

    // ====================================================================
    // SSE 连接
    // ====================================================================

    _initSSE() {
        this.sse = new SSEClient('/api/trace/stream', {
            onEvent: (data) => this._onTraceEvent(data),
            onStatus: (status) => this._onConnStatus(status),
            onError: (err) => console.warn('SSE error:', err),
        });
        this.sse.connect();
    },

    _onTraceEvent(event) {
        // 状态：双 Agent 对比数据从 llm_response 和 converge 中提取
        // 实际场景中，present_directive 收敛后的 llm_response 可能包含
        // 导演手记，Narrator 的演播文本在 pipeline 落库后可从 recent_turns 读取
        // 此处简化：通过 converge 事件附带的手记片段展示
        this.traceViewer.pushEvent(event);
    },

    _onConnStatus(status) {
        const el = this._connStatus;
        el.textContent = status === 'connected' ? '● 已连接' :
            status === 'connecting' ? '● 连接中' : '● 已断开';
        el.className = 'conn-status ' + status;
    },

    // ====================================================================
    // 时钟
    // ====================================================================

    _startClock() {
        const update = () => {
            const now = new Date();
            this._statusTime.textContent = now.toLocaleTimeString('zh-cn');
        };
        update();
        setInterval(update, 10000);
    },
};

// ====================================================================
// 入口
// ====================================================================

document.addEventListener('DOMContentLoaded', () => App.init());