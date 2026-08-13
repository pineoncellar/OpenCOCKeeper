/*- encoding: utf-8 -*/
/*
@File     :   trace_viewer.js
@Desc     :   Trace 面板渲染组件 — 时间线 + 工具链 + 双 Agent 对比
@Note     :   接收 SSE TraceEvent 流，增量渲染当前 Turn 的决策树
*/

class TraceViewer {
    constructor(container) {
        this.timelineEl = document.getElementById('timeline-list');
        this.chainEl = document.getElementById('trace-chain');
        this.emptyEl = document.getElementById('trace-empty');
        this.dualEl = document.getElementById('trace-dual');
        this.directiveEl = document.getElementById('directive-content');
        this.narrationEl = document.getElementById('narration-content');
        this.statusEvents = document.getElementById('status-events');
        this.statusTurns = document.getElementById('status-turns');
        this.statusTools = document.getElementById('status-tools');

        // 状态：按 (world_id, turn_num) 分组的事件
        this.turns = {};           // { key: { world_id, turn_num, events, tools, converged } }
        this.turnOrder = [];       // 有序 [key] 列表
        this.worldOrder = [];      // 有序 [world_id] 列表（用于时间线分组标题）
        this.currentKey = null;    // 当前选中的 key
        this.eventCount = 0;
        this.chainNodes = [];      // 当前展示链的节点

        // 状态：当前 turn 的半结构化数据
        this._currentPlayerInput = '';
        this._currentDirective = '';
        this._currentNarration = '';
        this._currentToolCalls = [];
    }

    // ====================================================================
    // 工具：key 生成与归属
    // ====================================================================

    _keyOf(worldId, turnNum) {
        return `${worldId || 'unknown'}|${turnNum || 0}`;
    }

    _registerWorld(worldId) {
        const w = worldId || 'unknown';
        if (!this.worldOrder.includes(w)) {
            this.worldOrder.push(w);
        }
    }

    // ====================================================================
    // 重置（世界筛选切换时调用）
    // ====================================================================

    reset() {
        this.turns = {};
        this.turnOrder = [];
        this.worldOrder = [];
        this.currentKey = null;
        this.eventCount = 0;
        this.chainNodes = [];
        this.statusEvents.textContent = 'Events: 0';
        this.statusTurns.textContent = 'Turns: 0';
        this.statusTools.textContent = 'Tools: 0';
        this.timelineEl.innerHTML = '';
        this.chainEl.innerHTML = '';
        this.chainEl.style.display = 'none';
        this.emptyEl.style.display = 'block';
        this.dualEl.style.display = 'none';
        this.directiveEl.textContent = '';
        this.narrationEl.textContent = '';
    }

    // ====================================================================
    // 事件入口
    // ====================================================================

    pushEvent(event) {
        this.eventCount++;
        this.statusEvents.textContent = `Events: ${this.eventCount}`;

        const worldId = event.world_id || 'unknown';
        const turn = event.turn_num || 0;
        const key = this._keyOf(worldId, turn);
        this._registerWorld(worldId);

        if (!this.turns[key]) {
            this.turns[key] = {
                world_id: worldId,
                turn_num: turn,
                events: [],
                tools: 0,
                converged: false,
            };
            this.turnOrder.push(key);
            this._sortTurnOrder();
            this._renderTimeline();
        }
        this.turns[key].events.push(event);
        this.statusTurns.textContent = `Turns: ${this.turnOrder.length}`;

        // 状态：自动切换到当前最新 turn
        if (this.currentKey === null || this._orderOf(key) >= this._orderOf(this.currentKey)) {
            this.selectKey(key);
        }
    }

    _sortTurnOrder() {
        this.turnOrder.sort((a, b) => {
            const ta = this.turns[a], tb = this.turns[b];
            // 状态：先按世界顺序，再按轮次号
            const wa = this.worldOrder.indexOf(ta.world_id);
            const wb = this.worldOrder.indexOf(tb.world_id);
            if (wa !== wb) return wa - wb;
            return ta.turn_num - tb.turn_num;
        });
    }

    _orderOf(key) {
        return this.turnOrder.indexOf(key);
    }

    // ====================================================================
    // Turn 选择
    // ====================================================================

    selectKey(key) {
        this.currentKey = key;
        this._renderTimeline();
        this._renderChain(key);
    }

    // ====================================================================
    // 渲染时间线（按世界分组）
    // ====================================================================

    _renderTimeline() {
        this.timelineEl.innerHTML = '';
        let lastWorld = null;
        for (const key of this.turnOrder) {
            const data = this.turns[key];

            // 状态：世界切换时插入分组标题
            if (data.world_id !== lastWorld) {
                lastWorld = data.world_id;
                const header = document.createElement('div');
                header.className = 'timeline-world-header';
                header.textContent = `◈ ${data.world_id}`;
                this.timelineEl.appendChild(header);
            }

            const item = document.createElement('div');
            item.className = 'timeline-item' + (key === this.currentKey ? ' active' : '');
            item.dataset.key = key;

            let label = `Turn ${data.turn_num}`;
            const toolCount = data.tools || 0;
            if (toolCount > 0) {
                label += ` <span class="turn-tools">${toolCount} tools</span>`;
            }
            if (data.converged) {
                label += ' <span class="turn-badge converged">✓</span>';
            }
            item.innerHTML = label;

            item.addEventListener('click', () => this.selectKey(key));
            this.timelineEl.appendChild(item);
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
        this._currentNarration = '';
        this._currentToolCalls = [];

        let toolCallsInTurn = 0;
        let converged = false;

        for (const event of events) {
            switch (event.event_type) {
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
                    // 状态：导演手记落位，渲染结束时并入双 Agent 对比区
                    this._currentDirective = (event.data || {}).directive || '';
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

        // 状态：如有双 Agent 数据则显示对比区
        if (this._currentDirective || this._currentNarration) {
            this.dualEl.style.display = 'flex';
            this.directiveEl.textContent = this._currentDirective || '(无手记)';
            this.narrationEl.textContent = this._currentNarration || '(无演播)';
        }
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
            // 状态：LLM 返回了工具调用，tool_call 事件会单独渲染
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
        node.textContent = `🎯 收敛 — ${data.reason || ''} (工具调用: ${data.tool_calls_count || 0})`;
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