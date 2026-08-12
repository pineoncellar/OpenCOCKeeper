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

        // 状态：按 turn 分组的事件
        this.turns = {};           // { turn_num: { events: [], tools: 0, converged: false } }
        this.turnOrder = [];       // 有序 turn 列表
        this.currentTurn = null;
        this.eventCount = 0;
        this.chainNodes = [];      // 当前展示链的节点

        // 状态：当前 turn 的半结构化数据
        this._currentPlayerInput = '';
        this._currentDirective = '';
        this._currentNarration = '';
        this._currentToolCalls = [];
    }

    // ====================================================================
    // 事件入口
    // ====================================================================

    pushEvent(event) {
        this.eventCount++;
        this.statusEvents.textContent = `Events: ${this.eventCount}`;

        const turn = event.turn_num || 0;
        if (!this.turns[turn]) {
            this.turns[turn] = { events: [], tools: 0, converged: false };
            this.turnOrder.push(turn);
            this.turnOrder.sort((a, b) => a - b);
            this._renderTimeline();
        }
        this.turns[turn].events.push(event);
        this.statusTurns.textContent = `Turns: ${this.turnOrder.length}`;

        // 状态：自动切换到当前最新 turn
        if (this.currentTurn === null || turn >= this.currentTurn) {
            this.selectTurn(turn);
        }
    }

    // ====================================================================
    // Turn 选择
    // ====================================================================

    selectTurn(turnNum) {
        this.currentTurn = turnNum;
        this._renderTimeline();
        this._renderChain(turnNum);
    }

    // ====================================================================
    // 渲染时间线
    // ====================================================================

    _renderTimeline() {
        this.timelineEl.innerHTML = '';
        for (const turn of this.turnOrder) {
            const data = this.turns[turn];
            const item = document.createElement('div');
            item.className = 'timeline-item' + (turn === this.currentTurn ? ' active' : '');
            item.dataset.turn = turn;

            let label = `Turn ${turn}`;
            const toolCount = data.tools || 0;
            if (toolCount > 0) {
                label += ` <span class="turn-tools">${toolCount} tools</span>`;
            }
            if (data.converged) {
                label += ' <span class="turn-badge converged">✓</span>';
            }
            item.innerHTML = label;

            item.addEventListener('click', () => this.selectTurn(turn));
            this.timelineEl.appendChild(item);
        }
    }

    // ====================================================================
    // 渲染工具链
    // ====================================================================

    _renderChain(turnNum) {
        const events = this.turns[turnNum]?.events || [];
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
            }
        }

        // 状态：更新统计数据
        this.turns[turnNum].tools = toolCallsInTurn;
        this.turns[turnNum].converged = converged;
        this.statusTools.textContent = `Tools: ${this.eventCount}`;

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
        node.textContent = `LLM 请求 tier=${data.tier} tools=${data.tool_names ? data.tool_names.join(', ') : '无'}`;
        this.chainEl.appendChild(node);
    }

    _renderLLMResponse(event) {
        const data = event.data || {};
        if (data.tool_calls && data.tool_calls.length > 0) {
            // 状态：LLM 返回了工具调用，tool_call 事件会单独渲染
            return;
        }
        if (data.content) {
            // 状态：可能是最终收敛文本，也可能是中间态的普通响应
            // 留待收敛事件处理
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