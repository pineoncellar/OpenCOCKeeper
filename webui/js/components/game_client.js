/*- encoding: utf-8 -*/
/*
@File     :   game_client.js
@Desc     :   图形化跑团终端 — WebSocket 对话 + 场景报幕 + 检定卡片 + 角色面板
@Note     :   连接 /ws/game；出站帧分派（narrative/system_message/session_info/
             state_diff）；解析 Narrator 首行报幕 [地点-区域-时间/天气] 与
             检定文本 "检定：骰值/阈值 等级"，渲染高亮卡片；角色面板随
             state_diff 推送实时刷新血条/背包，无需轮询
*/

class GameClient {
    constructor(container) {
        this.container = container;
        this.ws = null;
        this.entities = [];      // 当前世界角色快照
        this.worldId = null;
        this.statusEl = null;
    }

    init() {
        this.container.innerHTML = `
            <div class="game-toolbar">
                <div class="game-status" id="game-status"></div>
                <label class="notify-toggle" title="叙事完成提醒：页面在后台时通过标签页标题闪烁+提示音提醒你（纯网页前端，局域网访问同样有效）">
                    <input type="checkbox" id="game-notify"> 后台提醒
                </label>
            </div>
            <div class="game-layout" id="game-layout">
                <div class="game-main">
                    <div class="game-banner" id="game-banner">
                        <span class="game-banner-placeholder">尚未开始跑团</span>
                    </div>
                    <div class="game-log" id="game-log"></div>
                    <div class="game-input-row">
                        <textarea id="game-input" rows="1" placeholder="输入行动... 或 /命令（Ctrl+Enter / Shift+Enter 换行）" autocomplete="off" spellcheck="false"></textarea>
                        <button class="game-btn" id="game-send">发送</button>
                    </div>
                    <div class="game-quick">
                        <button class="game-chip" data-cmd="/help">/help</button>
                        <button class="game-chip" data-cmd="/status">/status</button>
                        <button class="game-chip" data-cmd="/world list">/world list</button>
                        <button class="game-chip" data-cmd="/module list">/module list</button>
                        <button class="game-chip" data-cmd="/card list">/card list</button>
                        <button class="game-chip" data-cmd="/rollback">/rollback</button>
                        <button class="game-chip" data-cmd="/world archive">/world archive</button>
                        <button class="game-chip" data-fill="/world start ">/world start</button>
                        <button class="game-chip" data-fill="/world load ">/world load</button>
                        <button class="game-chip" data-fill="/world delete ">/world delete</button>
                        <button class="game-chip" data-fill="/card import ">/card import</button>
                        <button class="game-chip" data-fill="/card use ">/card use</button>
                        <button class="game-chip" data-fill="/memory ">/memory</button>
                    </div>
                </div>
                <aside class="game-panel" id="game-panel">
                    <p class="placeholder">载入世界后显示角色面板</p>
                </aside>
            </div>
            <div class="trace-sidebar-mask" id="game-sidebar-mask"></div>
            <button class="trace-fab" id="game-fab-panel" title="角色面板" aria-label="角色面板">👤</button>`;

        this.statusEl = this.container.querySelector('#game-status');
        this.logEl = this.container.querySelector('#game-log');
        this.bannerEl = this.container.querySelector('#game-banner');
        this.panelEl = this.container.querySelector('#game-panel');
        this.inputEl = this.container.querySelector('#game-input');

        this.container.querySelector('#game-send').addEventListener('click', () => this._sendInput());
        // 状态：Enter 无修饰键=发送消息；Ctrl+Enter / Shift+Enter / Meta+Enter（含组合）=
        // 手动在光标处插入换行——textarea 中 Shift+Enter 默认换行但 Ctrl+Enter 无默认行为，
        // 统一走 _insertNewline 手动插入，保证三种修饰键行为一致
        this.inputEl.addEventListener('keydown', (ev) => {
            if (ev.key !== 'Enter') return;
            if (!ev.ctrlKey && !ev.metaKey && !ev.shiftKey) {
                ev.preventDefault();
                this._sendInput();
            } else {
                ev.preventDefault();
                this._insertNewline();
            }
        });
        // 状态：叙事完成后台提醒（TurnNotifier，设置存 localStorage）
        TurnNotifier.init();
        const notifyEl = this.container.querySelector('#game-notify');
        if (notifyEl) {
            notifyEl.checked = TurnNotifier.enabled;
            notifyEl.addEventListener('change', () => {
                TurnNotifier.enabled = notifyEl.checked;
                localStorage.setItem('coc.notify.enabled', String(notifyEl.checked));
            });
        }
        this.container.querySelectorAll('.game-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                if (chip.dataset.fill) {
                    // 状态：带参命令模板——填入输入框由玩家补全参数后回车发送
                    this.inputEl.value = chip.dataset.fill;
                    this.inputEl.focus();
                } else {
                    this._sendCommand(chip.dataset.cmd);
                }
            });
        });

        this._connect();
        this._initMobileSidebar();
    }

    // ====================================================================
    // 移动端：角色面板覆盖式抽屉（右侧滑入）
    // ====================================================================

    _initMobileSidebar() {
        const layout = this.container.querySelector('#game-layout');
        const mask = this.container.querySelector('#game-sidebar-mask');
        const fab = this.container.querySelector('#game-fab-panel');
        if (!layout || !mask || !fab) return;
        this._panelDrawer = initMobileDrawer({
            layout, mask, button: fab, drawer: this.panelEl,
            openClass: 'sidebar-panel',
        });
    }

    // ====================================================================
    // WebSocket 连接
    // ====================================================================

    _connect() {
        this.ws = new WSClient('/ws/game', {
            onFrame: (frame) => this._onFrame(frame),
            onStatus: (status) => {
                if (!this.statusEl) return;
                this.statusEl.textContent = status === 'connected' ? '● 已连接' :
                    status === 'connecting' ? '● 连接中' : '● 已断开';
                this.statusEl.className = 'game-status ' + status;
            },
        });
        this.ws.connect();
    }

    // ====================================================================
    // 出站帧分派
    // ====================================================================

    _onFrame(frame) {
        switch (frame.type) {
            case 'narrative':
                this._appendNarration(frame.text);
                break;
            case 'system_message':
                this._appendSystem(frame.text, frame.level || 'info');
                break;
            case 'session_info':
                this._loadSession(frame.data);
                break;
            case 'state_diff':
                this._onStateDiff(frame);
                break;
        }
    }

    _onStateDiff(frame) {
        // 状态：更新角色快照 + 渲染检定卡片
        // 注意：不在此渲染 narration——玩家视角叙事已由 narrative 帧（send 推送）
        // 单独展示，state_diff.narration 是落库广播的附带副本，若再渲染会整段重复
        if (Array.isArray(frame.entities) && frame.entities.length > 0) {
            this.entities = frame.entities;
            this._renderPanel();
        }
        if (Array.isArray(frame.checks) && frame.checks.length > 0) {
            frame.checks.forEach(c => this._appendCheck(c));
        }
    }

    _loadSession(data) {
        if (!data) return;
        this.worldId = data.world_id || null;
        if (Array.isArray(data.entities)) this.entities = data.entities;
        if (data.world_id) {
            this.bannerEl.innerHTML = `<span class="game-banner-module">${this._esc(data.module_name || data.world_id)}</span>`;
        }
        this._renderPanel();
    }

    // ====================================================================
    // 消息渲染
    // ====================================================================

    _appendNarration(text) {
        if (!text) return;
        const block = document.createElement('div');
        block.className = 'game-msg narrative';
        block.innerHTML = this._renderRichNarration(text);
        this.logEl.appendChild(block);
        this._maybeSceneBanner(text);
        this._scrollBottom();
        // 状态：叙事完成——页面在后台时触发提醒（标题闪烁 + 提示音）
        TurnNotifier.notify();
    }

    _renderRichNarration(text) {
        // 状态：检定行高亮：形如 "侦查检定：23/80 困难成功"；
        // 换行由 .game-msg 的 white-space: pre-wrap 保留，无需转 <br>
        let html = this._esc(text);
        html = html.replace(
            /([^\n]+检定)：\s*(\d+)\s*\/\s*(\d+)\s*(成功|失败)?/g,
            (m, name, roll, target, rank) =>
                `<span class="check-inline">🎲 ${this._esc(name)}：${roll}/${target} ${this._esc(rank || '')}</span>`
        );
        return html;
    }

    _appendCheck(check) {
        const name = check.name || '';
        const roll = check.roll_value ?? '?';
        const target = check.target_value ?? '?';
        const rank = check.rank || '';
        const block = document.createElement('div');
        block.className = 'game-msg check-card';
        block.innerHTML = `
            <span class="check-dice">d100</span>
            <div class="check-body">
                <div class="check-name">${this._esc(name)}</div>
                <div class="check-result">
                    <span class="check-roll">${this._esc(String(roll))}</span>
                    <span class="check-slash">/</span>
                    <span class="check-target">${this._esc(String(target))}</span>
                    <span class="check-rank">${this._esc(rank || '')}</span>
                </div>
            </div>`;
        this.logEl.appendChild(block);
        this._scrollBottom();
    }

    _appendSystem(text, level) {
        const block = document.createElement('div');
        block.className = `game-msg system ${level}`;
        block.textContent = text;
        this.logEl.appendChild(block);
        this._scrollBottom();
    }

    _maybeSceneBanner(text) {
        // 状态：抓首行 [地点-区域-时间/天气] 报幕渲染到顶部横幅
        const m = text.match(/^\s*\[([^\]]+)\]/);
        if (m) {
            this.bannerEl.innerHTML = `<span class="game-banner-scene">${this._esc(m[1])}</span>`;
        }
    }

    _scrollBottom() {
        this.logEl.scrollTop = this.logEl.scrollHeight;
    }

    // ====================================================================
    // 角色面板
    // ====================================================================

    _renderPanel() {
        if (this.entities.length === 0) {
            this.panelEl.innerHTML = '<p class="placeholder">载入世界后显示角色面板</p>';
            return;
        }
        const cards = this.entities.map(e => `
            <div class="game-entity-card">
                <div class="game-entity-name">${this._esc(e.name || e.id)}</div>
                ${this._bar('HP', e.hp, e.hp_max, 'hp')}
                ${this._bar('SAN', e.san, e.san_max, 'san')}
                ${this._bar('MP', e.mp, e.mp_max, 'mp')}
                ${this._renderTags(e.tags)}
                ${this._renderInventory(e.inventory)}
            </div>`).join('');
        this.panelEl.innerHTML = cards;
    }

    _bar(label, cur, max, cls) {
        const safeMax = Math.max(1, max || cur || 1);
        const pct = Math.max(0, Math.min(100, ((cur || 0) / safeMax) * 100));
        return `
            <div class="game-stat">
                <span class="game-stat-label ${cls}">${label}</span>
                <div class="game-bar"><div class="game-bar-fill ${cls}" style="width:${pct}%"></div></div>
                <span class="game-stat-val">${cur || 0}/${max ?? cur ?? 0}</span>
            </div>`;
    }

    _renderTags(tags) {
        if (!tags || tags.length === 0) return '';
        const chips = tags.map(t => `<span class="game-tag">${this._esc(t)}</span>`).join('');
        return `<div class="game-tags">${chips}</div>`;
    }

    _renderInventory(inventory) {
        if (!inventory || inventory.length === 0) return '';
        const items = inventory.map(i => `<span class="game-item">${this._esc(i.name || i)}</span>`).join('');
        return `<div class="game-inventory"><label>背包</label><div>${items}</div></div>`;
    }

    // ====================================================================
    // 输入
    // ====================================================================

    _insertNewline() {
        // 状态：在光标处插入换行（Ctrl/Meta/Shift+Enter 统一走此路径），
        // 光标右移一位；选中区域整体替换为单个换行（多行在框内滚动显示）
        const el = this.inputEl;
        const start = el.selectionStart ?? el.value.length;
        const end = el.selectionEnd ?? el.value.length;
        el.value = el.value.slice(0, start) + '\n' + el.value.slice(end);
        const pos = start + 1;
        el.selectionStart = el.selectionEnd = pos;
    }

    _sendInput() {
        const text = this.inputEl.value.trim();
        if (!text) return;
        // 状态：本地立即显示玩家消息，随后等叙事回显
        this._appendPlayer(text);
        this.ws.send({ type: text.startsWith('/') ? 'system_cmd' : 'player_input', text });
        this.inputEl.value = '';
    }

    _sendCommand(cmd) {
        this._appendPlayer(cmd);
        this.ws.send({ type: 'system_cmd', text: cmd });
    }

    _appendPlayer(text) {
        const block = document.createElement('div');
        block.className = 'game-msg player';
        block.textContent = text;
        this.logEl.appendChild(block);
        this._scrollBottom();
    }

    // ====================================================================
    // 工具
    // ====================================================================

    _esc(text) {
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }
}
