/*- encoding: utf-8 -*/
/*
@File     :   trace_viewer.js
@Desc     :   Trace 面板渲染组件 — 世界/轮次两级树 + 单轮工具链 + 双 Agent 对比
@Note     :   左侧世界列表经 REST /api/trace/worlds 加载（重启后可读回历史），
             选中世界后轮次列表走 /api/trace/worlds/{id}/turns 分页；
             点轮次经 /api/trace/worlds/{id}/turns/{num} 拉单轮事件渲染详情；
             SSE 实时事件经 pushEvent 增量合并进对应轮次（按时间戳+类型去重）
*/

class TraceViewer {
    constructor(container) {
        this.worldsListEl = document.getElementById('worlds-list');
        this.timelineEl = document.getElementById('timeline-list');
        this.loadMoreBtn = document.getElementById('trace-load-more');
        this.chainEl = document.getElementById('trace-chain');
        this.emptyEl = document.getElementById('trace-empty');
        this.dualEl = document.getElementById('trace-dual');
        this.directiveEl = document.getElementById('directive-content');
        this.narrationEl = document.getElementById('narration-content');
        this.statusEvents = document.getElementById('status-events');
        this.statusTurns = document.getElementById('status-turns');
        this.statusTools = document.getElementById('status-tools');

        // 世界/轮次两级树状态
        this.worlds = [];                 // [{world_id, turn_count, latest_turn, latest_ts}]
        this.selectedWorld = '';          // 当前选中世界（'' = 未选）
        this.turns = {};                  // key -> {world_id, turn_num, events, tools, converged, _meta}
        this.turnOrder = [];              // 有序 [key]（最新在前）
        this.currentKey = null;           // 当前选中轮次 key
        this.eventCount = 0;
        this.chainNodes = [];             // 当前展示链的节点
        this._turnTotal = 0;              // 该世界总轮数（翻页判底）
        this._loadedKeys = new Set();     // REST 已加载过的轮 key（避免重复拉取）
        this.pageSize = 20;               // 每页轮数（对齐 config.webui.trace.default_turns）

        // 状态：当前 turn 的半结构化数据
        this._currentPlayerInput = '';
        this._currentDirective = '';
        this._currentNarration = '';
        this._currentToolCalls = [];

        // 外部回调：世界选择变化（App 借此重连 SSE 过滤并同步顶栏下拉）
        this.onWorldChange = null;
    }

    // ====================================================================
    // 工具：key 生成与事件合并
    // ====================================================================

    _keyOf(worldId, turnNum) {
        return `${worldId || 'unknown'}|${turnNum || 0}`;
    }

    _eventKey(ev) {
        const d = ev.data || {};
        return `${ev.timestamp}|${ev.event_type}|${d.name || ''}`;
    }

    _mergeEvents(existing, incoming) {
        const seen = new Set(existing.map(e => this._eventKey(e)));
        const merged = existing.slice();
        for (const ev of incoming) {
            const k = this._eventKey(ev);
            if (!seen.has(k)) {
                seen.add(k);
                merged.push(ev);
            }
        }
        // 状态：保持时间序（同一轮内事件先后即 trace 顺序）
        merged.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
        return merged;
    }

    _showEmpty(msg) {
        this.emptyEl.style.display = 'flex';
        this.chainEl.style.display = 'none';
        this.dualEl.style.display = 'none';
        const p = this.emptyEl.querySelector('p');
        if (p && msg) p.textContent = msg;
    }

    // ====================================================================
    // 初始化与世界列表（REST 历史 + SSE 增量刷新）
    // ====================================================================

    async init() {
        await this.loadWorlds();
        if (this.loadMoreBtn) {
            this.loadMoreBtn.addEventListener('click', () => this.loadTurns(this.turnOrder.length));
        }
        // 状态：移动端覆盖式侧栏（世界/轮次抽屉）交互，桌面端自动跳过
        this._initMobileSidebar();
    }

    // ====================================================================
    // 移动端：覆盖式侧栏（世界/轮次抽屉）
    // ====================================================================

    _initMobileSidebar() {
        const layout = document.getElementById('trace-layout');
        const mask = document.getElementById('trace-sidebar-mask');
        if (!layout || !mask) return;   // 元素缺失（桌面端）时跳过

        // 状态：世界/轮次两个抽屉互斥——打开一个先关闭另一个
        this._worldDrawer = initMobileDrawer({
            layout, mask,
            button: document.getElementById('trace-fab-worlds'),
            drawer: this.worldsListEl,
            openClass: 'sidebar-worlds',
            onOpen: () => this._timelineDrawer && this._timelineDrawer.close(),
            onSelect: (e) => !!e.target.closest('.world-item'),
        });
        this._timelineDrawer = initMobileDrawer({
            layout, mask,
            button: document.getElementById('trace-fab-timeline'),
            drawer: this.timelineEl,
            openClass: 'sidebar-timeline',
            onOpen: () => this._worldDrawer && this._worldDrawer.close(),
            onSelect: (e) => !!e.target.closest('.timeline-item'),
        });
    }

    async loadWorlds() {
        try {
            const data = await api.get('/api/trace/worlds');
            this.worlds = (data.worlds || []).slice();
            this._renderWorlds();
        } catch (e) {
            console.warn('加载世界列表失败:', e);
        }
    }

    _clipSceneNotes(text, limit = 36) {
        // 状态：折叠空白并截断，标题提示（title）保留全文供悬停查看
        const s = (text || '').replace(/\s+/g, ' ').trim();
        return s.length > limit ? s.slice(0, limit) + '…' : s;
    }

    _renderWorlds() {
        if (!this.worldsListEl) return;
        this.worldsListEl.innerHTML = '';
        if (this.worlds.length === 0) {
            const empty = document.createElement('div');
            empty.className = 'worlds-empty';
            empty.textContent = '暂无 trace 记录';
            this.worldsListEl.appendChild(empty);
            return;
        }
        for (const w of this.worlds) {
            const item = document.createElement('div');
            item.className = 'world-item' + (w.world_id === this.selectedWorld ? ' active' : '');
            item.dataset.world = w.world_id;
            const name = document.createElement('div');
            name.className = 'world-name';
            name.textContent = w.world_id;
            const meta = document.createElement('div');
            meta.className = 'world-meta';
            meta.textContent = `${w.turn_count} 轮` + (w.latest_turn != null ? ` · 至 Turn ${w.latest_turn}` : '');
            item.appendChild(name);
            item.appendChild(meta);
            // 状态：当前场景手记（KP 局部手记）——截断展示、悬停看全文，空则不渲染
            if (w.scene_notes) {
                const note = document.createElement('div');
                note.className = 'world-scene-notes';
                note.textContent = this._clipSceneNotes(w.scene_notes);
                note.title = w.scene_notes;
                item.appendChild(note);
            }
            item.addEventListener('click', () => this.selectWorld(w.world_id));
            this.worldsListEl.appendChild(item);
        }
    }

    async selectWorld(worldId) {
        if (this.selectedWorld === worldId && this.turnOrder.length > 0) {
            return;   // 状态：已选且已加载，避免重复重置
        }
        this.selectedWorld = worldId;
        // 状态：清空轮次与详情，切到新世界
        this.turns = {};
        this.turnOrder = [];
        this.currentKey = null;
        this._loadedKeys.clear();
        this._turnTotal = 0;
        this._renderWorlds();
        this._showEmpty(`世界 ${worldId} 的轮次 trace 已就绪，点选轮次查看详情`);
        await this.loadTurns(0);
        // 状态：通知 App 重连 SSE（按世界过滤）并同步顶栏下拉
        if (this.onWorldChange) this.onWorldChange(worldId);
    }

    // ====================================================================
    // 轮次列表（REST 分页）与轮次详情（REST 历史 + 本地合并）
    // ====================================================================

    async loadTurns(offset = 0) {
        if (!this.selectedWorld) return;
        try {
            const data = await api.get(
                `/api/trace/worlds/${encodeURIComponent(this.selectedWorld)}/turns?limit=${this.pageSize}&offset=${offset}`
            );
            this._turnTotal = data.total || 0;
            // 状态：元信息并入 turns（事件数组延迟到 selectTurn 再拉）
            for (const t of (data.turns || [])) {
                const key = this._keyOf(this.selectedWorld, t.turn_num);
                if (!this.turns[key]) {
                    this.turns[key] = {
                        world_id: this.selectedWorld,
                        turn_num: t.turn_num,
                        events: [],
                        tools: 0,
                        converged: false,
                        loaded: false,
                    };
                    this.turnOrder.push(key);
                }
                this.turns[key]._meta = t;
            }
            // 状态：轮次倒序（最新在前），与后端 list_turns 一致
            this.turnOrder.sort((a, b) => {
                const ta = this.turns[a], tb = this.turns[b];
                if (ta.world_id !== tb.world_id) return 0;
                return tb.turn_num - ta.turn_num;
            });
            this.statusTurns.textContent = `Turns: ${this.turnOrder.length}/${this._turnTotal}`;
            this._renderTimeline();
            // 状态：还有更早轮次才显示加载更多
            if (this.loadMoreBtn) {
                this.loadMoreBtn.style.display = this.turnOrder.length < this._turnTotal ? 'block' : 'none';
            }
        } catch (e) {
            console.warn('加载轮次列表失败:', e);
        }
    }

    _renderTimeline() {
        this.timelineEl.innerHTML = '';
        for (const key of this.turnOrder) {
            const data = this.turns[key];
            const meta = data._meta || {};
            const item = document.createElement('div');
            item.className = 'timeline-item' + (key === this.currentKey ? ' active' : '');
            item.dataset.key = key;
            let label = `Turn ${data.turn_num}`;
            if (meta.event_count) label += ` <span class="turn-tools">${meta.event_count} ev</span>`;
            if (data.converged) label += ' <span class="turn-badge converged">✓</span>';
            item.innerHTML = label;
            item.addEventListener('click', () => this.selectTurn(data.world_id, data.turn_num));
            this.timelineEl.appendChild(item);
        }
    }

    async selectTurn(worldId, turnNum) {
        const key = this._keyOf(worldId, turnNum);
        if (!this.turns[key]) {
            this.turns[key] = {
                world_id: worldId, turn_num: turnNum,
                events: [], tools: 0, converged: false, loaded: false,
            };
            this.turnOrder.push(key);
        }
        // 状态：历史未拉取过才走 REST（已加载的轮靠 SSE 增量继续）
        if (!this._loadedKeys.has(key)) {
            this._loadedKeys.add(key);
            try {
                const data = await api.get(
                    `/api/trace/worlds/${encodeURIComponent(worldId)}/turns/${turnNum}`
                );
                this.turns[key].events = this._mergeEvents(this.turns[key].events, data.events || []);
            } catch (e) {
                console.warn('加载轮次 trace 失败:', e);
            }
        }
        this.selectKey(key);
    }

    selectKey(key) {
        this.currentKey = key;
        this._renderTimeline();
        this._renderChain(key);
    }

    // ====================================================================
    // SSE 实时增量
    // ====================================================================

    pushEvent(event) {
        this.eventCount++;
        this.statusEvents.textContent = `Events: ${this.eventCount}`;

        const worldId = event.world_id || 'unknown';
        const turn = event.turn_num || 0;
        const key = this._keyOf(worldId, turn);

        // 状态：SSE 出现未知世界 → 刷新世界列表（REST 为主，增量补新世界）
        if (!this.worlds.some(w => w.world_id === worldId)) {
            this.loadWorlds();
        }

        if (!this.turns[key]) {
            this.turns[key] = {
                world_id: worldId, turn_num: turn,
                events: [], tools: 0, converged: false, loaded: true,
            };
            this.turnOrder.push(key);
            this.statusTurns.textContent = `Turns: ${this.turnOrder.length}`;
        }
        // 状态：合并去重（时间戳+类型+工具名 作为近似键）
        this.turns[key].events = this._mergeEvents(this.turns[key].events, [event]);

        // 状态：当前正看该轮则增量重渲染详情，否则只刷新轮次列表
        if (this.currentKey === key) {
            this._renderChain(key);
        } else if (this.selectedWorld === worldId) {
            this._renderTimeline();
        }
    }

    // ====================================================================
    // 渲染工具链
    // ====================================================================

    _renderChain(key) {
        const data = this.turns[key];
        const events = data ? data.events : [];
        this.chainEl.innerHTML = '';
        this.chainNodes = [];
        this.emptyEl.style.display = 'none';
        this.chainEl.style.display = 'block';
        this.dualEl.style.display = 'none';

        // 状态：重置当前 turn 的累积数据
        this._currentPlayerInput = '';
        this._currentDirective = '';
        this._currentSceneNotes = '';
        this._currentNarration = '';
        this._currentToolCalls = [];

        let toolCallsInTurn = 0;
        let converged = false;

        for (const event of events) {
            switch (event.event_type) {
                case 'player_input':
                    this._renderPlayerInput(event);
                    break;
                case 'llm_request':
                    this._renderLLMRequest(event);
                    break;
                case 'llm_response':
                    this._renderLLMResponse(event);
                    break;
                case 'tool_call':
                    this._renderToolCall(event);
                    toolCallsInTurn++;
                    break;
                case 'tool_result':
                    this._renderToolResult(event);
                    break;
                case 'converge':
                    this._renderConverge(event);
                    converged = true;
                    break;
                case 'directive':
                    // 状态：导演手记落位（含可选场景手记），渲染结束时并入双 Agent 对比区
                    this._currentDirective = (event.data || {}).directive || '';
                    this._currentSceneNotes = (event.data || {}).scene_notes || '';
                    break;
                case 'narration':
                    // 状态：演播文本落位，渲染结束时并入双 Agent 对比区
                    this._currentNarration = (event.data || {}).narration || '';
                    break;
            }
        }

        // 状态：更新统计数据
        this.turns[key].tools = toolCallsInTurn;
        this.turns[key].converged = converged;
        this.statusTools.textContent = `Tools: ${toolCallsInTurn}`;

        // 状态：如有双 Agent 数据则显示对比区（导演手记后附场景手记小节）
        if (this._currentDirective || this._currentNarration) {
            this.dualEl.style.display = 'flex';
            let directiveText = this._currentDirective || '(无手记)';
            if (this._currentSceneNotes) {
                directiveText += '\n\n【场景手记】\n' + this._currentSceneNotes;
            }
            this.directiveEl.textContent = directiveText;
            this.narrationEl.textContent = this._currentNarration || '(无演播)';
        }
    }

    _renderPlayerInput(event) {
        const data = event.data || {};
        const node = document.createElement('div');
        node.className = 'chain-node player-input';
        node.innerHTML = `<span class="chain-tag">玩家输入</span> ${this._escapeHtml(data.action || '')}`;
        this.chainEl.appendChild(node);
        this.chainNodes.push(node);
    }

    _renderLLMRequest(event) {
        const data = event.data || {};
        const node = document.createElement('div');
        node.className = 'chain-node llm-request';
        const tools = data.tool_names && data.tool_names.length
            ? data.tool_names.join(', ') : '无';
        node.innerHTML = `LLM 请求 tier=${data.tier} tools=${tools}` +
            `<span class="chain-expand">展开提示词</span>`;
        // 状态：点击展开最终组装的完整 messages（调试图）
        this._attachExpand(node, '.chain-expand', '提示词', this._formatMessages(data.messages));
        this.chainEl.appendChild(node);
        this.chainNodes.push(node);
    }

    _renderLLMResponse(event) {
        const data = event.data || {};
        if (data.tool_calls && data.tool_calls.length > 0) {
            // 状态：中间步（ReAct 思考 + 工具调用）——tool_call 事件单独渲染，
            // 思考正文在此呈现，形成"先思考后行动"的阅读链（SSE 逐步推送下实时可见）
            this._renderThought(data);
            return;
        }
        if (data.content) {
            // 状态：普通文本响应（非工具调用）——可展开查看原始输出全文
            const node = document.createElement('div');
            node.className = 'chain-node llm-response';
            node.innerHTML = `<span class="chain-tag">LLM 响应</span>` +
                `<span class="chain-expand">展开输出</span>`;
            this._attachExpand(node, '.chain-expand', '输出', data.content);
            this.chainEl.appendChild(node);
            this.chainNodes.push(node);
        }
    }

    _renderThought(data) {
        // 思考正文：优先 content（模型主动输出的思考摘要），缺失退到 reasoning_content（推理链）
        const thought = data.content || '';
        const reasoning = data.reasoning_content || '';
        if (!thought && !reasoning) return;
        const node = document.createElement('div');
        node.className = 'chain-node thought';
        const tag = document.createElement('span');
        tag.className = 'chain-tag';
        tag.textContent = '思考';
        node.appendChild(tag);
        // 思考摘要直接展示
        if (thought) {
            const text = document.createElement('div');
            text.className = 'thought-text';
            text.textContent = thought;
            node.appendChild(text);
        }
        // 完整推理链折叠展开（调试复盘用；与摘要重复则不展示）
        if (reasoning && reasoning !== thought) {
            node.insertAdjacentHTML('beforeend',
                `<span class="chain-expand">展开推理链</span>`);
            this._attachExpand(node, '.chain-expand', '推理链', reasoning);
        }
        this.chainEl.appendChild(node);
        this.chainNodes.push(node);
    }

    _renderToolCall(event) {
        const data = event.data || {};
        this._currentToolCalls.push(data);

        const node = document.createElement('div');
        node.className = 'chain-node tool-call';
        node.innerHTML = `<span class="tool-name">${this._escapeHtml(data.name)}</span>` +
            `<span class="tool-args">${this._briefArgs(data.arguments)}</span>`;
        this.chainEl.appendChild(node);
        this.chainNodes.push(node);
    }

    _renderToolResult(event) {
        const data = event.data || {};
        const result = data.result || {};
        const ok = result.ok;

        const node = document.createElement('div');
        node.className = 'chain-node tool-result';
        node.innerHTML = `<span class="tool-badge ${ok ? 'ok' : 'fail'}">${ok ? 'OK' : 'FAIL'}</span> ` +
            `<span class="tool-name">${this._escapeHtml(data.name)}</span>`;

        // 状态：检索类工具展开命中结果
        if (result.hits && Array.isArray(result.hits)) {
            const hitList = document.createElement('div');
            hitList.style.marginTop = '4px';
            for (const hit of result.hits) {
                const hitItem = document.createElement('div');
                hitItem.className = 'hit-item';
                const score = (hit.score || 0).toFixed(2);
                const title = hit.title || '(无标题)';
                const content = (hit.content || '').slice(0, 120);
                hitItem.innerHTML = `[${score}] <strong>${this._escapeHtml(title)}</strong> — ${this._escapeHtml(content)}`;
                // 状态：点击展开全文
                hitItem.style.cursor = 'pointer';
                const fullContent = hit.content || '';
                hitItem.addEventListener('click', () => {
                    const detail = window.open('', '_blank', 'width=600,height=400');
                    detail.document.write(`<pre>${this._escapeHtml(fullContent)}</pre>`);
                });
                hitList.appendChild(hitItem);
            }
            node.appendChild(hitList);
        }

        // 状态：检定结果高亮
        if (result.check) {
            const check = result.check;
            const checkItem = document.createElement('div');
            checkItem.style.marginTop = '4px';
            checkItem.style.color = 'var(--accent-cyan)';
            checkItem.textContent = `检定: ${check.name || ''} ${check.roll_value || ''}/${check.target_value || ''} ${check.rank || ''}`;
            node.appendChild(checkItem);
        }

        this.chainEl.appendChild(node);
        this.chainNodes.push(node);
    }

    _renderConverge(event) {
        const data = event.data || {};
        const node = document.createElement('div');
        node.className = 'chain-node converge';
        node.textContent = `收敛 — ${data.reason || ''} (工具调用: ${data.tool_calls_count || 0})`;
        this.chainEl.appendChild(node);
        this.chainNodes.push(node);
    }

    // ====================================================================
    // 折叠展开辅助
    // ====================================================================

    _attachExpand(node, triggerSel, label, content) {
        const trigger = node.querySelector(triggerSel);
        if (!trigger || !content) return;
        trigger.style.cursor = 'pointer';
        trigger.addEventListener('click', () => {
            const panel = node.querySelector('.chain-prompt');
            if (panel) {
                // 状态：已展开则切换显隐
                const hidden = panel.style.display === 'none';
                panel.style.display = hidden ? 'block' : 'none';
                trigger.textContent = hidden ? `收起${label}` : `展开${label}`;
                return;
            }
            const box = document.createElement('div');
            box.className = 'chain-prompt';
            box.style.display = 'block';
            box.textContent = content;
            node.appendChild(box);
            trigger.textContent = `收起${label}`;
        });
    }

    _formatMessages(messages) {
        if (!Array.isArray(messages)) return '';
        const parts = [];
        for (const m of messages) {
            const role = m.role || '?';
            let body = typeof m.content === 'string' ? m.content
                : JSON.stringify(m.content ?? '', null, 2);
            if (m.tool_calls) {
                // 状态：工具调用消息附带 tool_calls 参数详情
                body = (body || '') + '\n\n' + JSON.stringify(m.tool_calls, null, 2);
            }
            parts.push(`【${role}】\n${body}`);
        }
        return parts.join('\n\n' + '-'.repeat(40) + '\n\n');
    }

    // ====================================================================
    // 外部接口：注入双 Agent 数据
    // ====================================================================

    setDualContent(directive, narration) {
        this._currentDirective = directive || '';
        this._currentSceneNotes = '';
        this._currentNarration = narration || '';
        if (this.dualEl) {
            this.dualEl.style.display = 'flex';
            this.directiveEl.textContent = this._currentDirective || '(无手记)';
            this.narrationEl.textContent = this._currentNarration || '(无演播)';
        }
    }

    // ====================================================================
    // 工具
    // ====================================================================

    _escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    _briefArgs(args) {
        if (!args) return '(无参数)';
        const parts = [];
        for (const [k, v] of Object.entries(args)) {
            const val = typeof v === 'string' ? v.slice(0, 40) : JSON.stringify(v).slice(0, 40);
            parts.push(`${k}=${val}`);
        }
        return parts.join(' ');
    }
}