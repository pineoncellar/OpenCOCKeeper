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
        // 状态：TraceViewer 选中世界时回调——同步顶栏下拉 + 重连 SSE 过滤
        this.traceViewer.onWorldChange = (worldId) => this._onTraceWorldChange(worldId);
        this.stateInspector = null;   // 状态：Worlds 面板懒初始化（首次点击时才建）
        this.configEditor = null;     // 状态：Config 面板懒初始化
        this.gameClient = null;       // 状态：Game 面板懒初始化
        this._inspectorInited = false;
        this._configInited = false;
        this._gameInited = false;
        this._connStatus = document.getElementById('conn-status');
        this._statusTime = document.getElementById('status-time');
        this._worldFilter = document.getElementById('world-filter');
        this._knownWorlds = new Set();   // 已见过的 world_id 集合（填充下拉框）
        this._currentWorldFilter = '';   // 当前筛选的 world_id（'' = 全部）

        this._initTabs();
        this._initWorldFilter();
        this._initSSE();
        // 状态：Trace 面板初始加载世界列表（REST 读回历史）
        this.traceViewer.init();
        this._startClock();
    },

    // 状态：TraceViewer 世界选择回调——同步下拉并重连 SSE（按世界过滤）
    _onTraceWorldChange(worldId) {
        this._currentWorldFilter = worldId || '';
        if (this._worldFilter) {
            this._worldFilter.value = this._currentWorldFilter;
        }
        this._initSSE();
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

            // 状态：Worlds 面板首次打开时懒初始化数据剖析器
            if (tab === 'worlds' && !this._inspectorInited) {
                this._inspectorInited = true;
                const container = document.getElementById('state-inspector-container');
                if (container) {
                    this.stateInspector = new StateInspector(container);
                    this.stateInspector.init();
                }
            }

            // 状态：Config 面板首次打开时懒初始化配置编辑器
            if (tab === 'config' && !this._configInited) {
                this._configInited = true;
                const container = document.getElementById('config-editor-container');
                if (container) {
                    this.configEditor = new ConfigEditor(container);
                    this.configEditor.init();
                }
            }

            // 状态：Game 面板首次打开时懒初始化跑团终端
            if (tab === 'game' && !this._gameInited) {
                this._gameInited = true;
                const container = document.getElementById('game-client-container');
                if (container) {
                    this.gameClient = new GameClient(container);
                    this.gameClient.init();
                }
            }
        });
    },

    // ====================================================================
    // 世界筛选
    // ====================================================================

    _initWorldFilter() {
        if (!this._worldFilter) return;
        this._worldFilter.addEventListener('change', () => {
            const value = this._worldFilter.value;
            if (value) {
                // 状态：下拉选中某世界 → 联动左侧列表选中并重连 SSE
                this.traceViewer.selectWorld(value);
            } else {
                // 状态：全部世界 → 仅重连 SSE（不过滤），左侧列表视图不动
                this._currentWorldFilter = '';
                this._initSSE();
            }
        });
    },

    _trackWorld(worldId) {
        // 状态：把新出现的 world_id 加入下拉框选项并刷新左侧世界列表（排除空 world）
        if (!worldId) return;
        if (!this._knownWorlds.has(worldId)) {
            this._knownWorlds.add(worldId);
            // 状态：SSE 出现新世界 → 刷新世界列表（REST 为主，增量补新）
            this.traceViewer.loadWorlds();
        }
        if (!this._worldFilter) return;
        const exists = Array.from(this._worldFilter.options).some(o => o.value === worldId);
        if (exists) return;
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