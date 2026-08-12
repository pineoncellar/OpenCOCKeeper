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
        this._worldFilter = document.getElementById('world-filter');
        this._knownWorlds = new Set();   // 已见过的 world_id 集合（填充下拉框）
        this._currentWorldFilter = '';   // 当前筛选的 world_id（'' = 全部）

        this._initTabs();
        this._initWorldFilter();
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
    // 世界筛选
    // ====================================================================

    _initWorldFilter() {
        if (!this._worldFilter) return;
        this._worldFilter.addEventListener('change', () => {
            this._currentWorldFilter = this._worldFilter.value;
            // 状态：重连 SSE 应用世界过滤（SSE 端点原生支持 ?world_id=）
            this._initSSE();
            // 状态：清空并重建 trace 视图，展示筛选后的事件
            this.traceViewer.reset();
        });
    },

    _trackWorld(worldId) {
        // 状态：把新出现的 world_id 加入下拉框选项（排除空 world）
        if (!worldId) return;
        if (this._knownWorlds.has(worldId)) return;
        this._knownWorlds.add(worldId);
        if (!this._worldFilter) return;
        const opt = document.createElement('option');
        opt.value = worldId;
        opt.textContent = `世界: ${worldId}`;
        this._worldFilter.appendChild(opt);
    },

    // ====================================================================
    // SSE 连接
    // ====================================================================

    _initSSE() {
        if (this.sse) {
            this.sse.disconnect();
        }
        // 状态：带世界过滤的连接 URL
        const url = this._currentWorldFilter
            ? `/api/trace/stream?world_id=${encodeURIComponent(this._currentWorldFilter)}`
            : '/api/trace/stream';
        this.sse = new SSEClient(url, {
            onEvent: (data) => this._onTraceEvent(data),
            onStatus: (status) => this._onConnStatus(status),
            onError: (err) => console.warn('SSE error:', err),
        });
        this.sse.connect();
    },

    _onTraceEvent(event) {
        // 状态：记录世界并转发给 TraceViewer
        this._trackWorld(event.world_id || '');
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