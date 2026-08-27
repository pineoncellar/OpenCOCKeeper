/*- encoding: utf-8 -*/
/*
@File     :   state_inspector.js
@Desc     :   数据底座剖析面板 — 世界列表 + 实体状态 + 轮次回档 + 记忆检索
@Note     :   接入 /api/worlds 系列 REST 端点；回档/删除为写操作，先弹确认再执行
*/

class StateInspector {
    constructor(container) {
        this.container = container;
        this.worldsEl = null;
        this.detailEl = null;
        this.statusEl = null;
        this.currentWorld = null;
    }

    init() {
        this.container.innerHTML = `
            <div class="inspector-status" id="inspector-status"></div>
            <div class="inspector-layout" id="inspector-layout">
                <aside class="inspector-worlds" id="inspector-worlds"></aside>
                <div class="inspector-detail" id="inspector-detail">
                    <p class="placeholder">选择一个世界查看详情</p>
                </div>
            </div>
            <div class="trace-sidebar-mask" id="inspector-sidebar-mask"></div>
            <button class="trace-fab" id="inspector-fab-worlds" title="世界列表" aria-label="世界列表">🗺️</button>`;
        this.worldsEl = this.container.querySelector('#inspector-worlds');
        this.detailEl = this.container.querySelector('#inspector-detail');
        this.statusEl = this.container.querySelector('#inspector-status');
        this._initMobileSidebar();
        this._loadWorlds();
    }

    // ====================================================================
    // 移动端：世界列表覆盖式抽屉（左侧滑入）
    // ====================================================================

    _initMobileSidebar() {
        const layout = this.container.querySelector('#inspector-layout');
        const mask = this.container.querySelector('#inspector-sidebar-mask');
        const fab = this.container.querySelector('#inspector-fab-worlds');
        if (!layout || !mask || !fab) return;
        // 状态：选中世界后自动收起世界列表抽屉
        this._worldsDrawer = initMobileDrawer({
            layout, mask, button: fab, drawer: this.worldsEl,
            openClass: 'sidebar-worlds',
            onSelect: (e) => !!e.target.closest('.inspector-world-item'),
        });
    }

    // ====================================================================
    // 世界列表
    // ====================================================================

    async _loadWorlds() {
        this._setStatus('加载世界列表...');
        try {
            const data = await api.get('/api/worlds');
            this.worlds = data.worlds || [];
            this._renderWorlds();
            this._setStatus(`共 ${this.worlds.length} 个世界`);
        } catch (e) {
            this._setStatus(`世界列表加载失败: ${e.message}`, 'error');
        }
    }

    _renderWorlds() {
        this.worldsEl.innerHTML = '<h3 class="panel-title">世界</h3>';
        const list = document.createElement('div');
        list.className = 'inspector-world-list';

        if (this.worlds.length === 0) {
            list.innerHTML = '<p class="inspector-empty">暂无世界</p>';
            this.worldsEl.appendChild(list);
            return;
        }

        for (const w of this.worlds) {
            const item = document.createElement('div');
            item.className = 'inspector-world-item' + (w.world_id === this.currentWorld ? ' active' : '');
            const archived = w.status === 'ARCHIVED';
            item.innerHTML = `
                <div class="inspector-world-name">
                    <span class="inspector-world-badge ${archived ? 'archived' : 'active'}">${w.status}</span>
                    ${this._esc(w.world_id)}
                </div>
                <div class="inspector-world-meta">
                    模组: ${this._esc(w.module_name || '无')} · 实体: ${w.entity_count || 0}
                </div>
                <div class="inspector-world-recap">${this._esc(w.global_recap || '')}</div>`;
            item.addEventListener('click', () => this._selectWorld(w.world_id));
            list.appendChild(item);
        }
        this.worldsEl.appendChild(list);
    }

    async _selectWorld(worldId) {
        this.currentWorld = worldId;
        this._renderWorlds();
        this._setStatus(`加载世界 ${worldId}...`);
        try {
            // 状态：先并行加载快速数据（世界 + 实体 + 轮次），记忆后台异步加载，
            // 避免真实 embedding 端点阻塞主面板渲染
            const [world, entities, turns] = await Promise.all([
                api.get(`/api/worlds/${encodeURIComponent(worldId)}`),
                api.get(`/api/worlds/${encodeURIComponent(worldId)}/entities`),
                api.get(`/api/worlds/${encodeURIComponent(worldId)}/turns`),
            ]);
            this.worldDetail = world.world;
            this.entities = entities.entities || [];
            this.turns = turns.turns || [];
            this.memories = [];
            this._renderDetail();
            this._setStatus(`世界 ${worldId} 已加载`);
            // 状态：记忆异步加载，不阻塞渲染（失败不影响主面板）
            this._loadMemories('');
        } catch (e) {
            this._setStatus(`世界加载失败: ${e.message}`, 'error');
        }
    }

    async _loadMemories(query) {
        if (this.currentWorld === null) return;
        const list = this.detailEl ? this.detailEl.querySelector('.inspector-memory-list') : null;
        if (list) list.innerHTML = '<p class="inspector-empty">记忆检索中...</p>';
        try {
            const q = (query || '').trim();
            const url = `/api/worlds/${encodeURIComponent(this.currentWorld)}/memories`
                + (q ? `?query=${encodeURIComponent(q)}` : '');
            const data = await api.get(url);
            this.memories = data.hits || [];
            this._reRenderMemories();
            // 状态：更新概览区的记忆条数
            const countEl = this.detailEl && this.detailEl.querySelector('#inspector-memory-count');
            if (countEl) countEl.textContent = this.memories.length;
        } catch (e) {
            if (list) list.innerHTML = `<p class="inspector-empty">记忆加载失败: ${this._esc(e.message)}</p>`;
        }
    }

    // ====================================================================
    // 详情渲染
    // ====================================================================

    _renderDetail() {
        const w = this.worldDetail || {};
        const recap = w.global_recap || '(无前情提要)';
        const recapShort = recap.length > 300 ? recap.slice(0, 300) + '…' : recap;

        this.detailEl.innerHTML = `
            <div class="inspector-detail-head">
                <h2>${this._esc(w.world_id || this.currentWorld)}</h2>
                <span class="inspector-world-badge ${w.status === 'ARCHIVED' ? 'archived' : 'active'}">${w.status || 'ACTIVE'}</span>
                <button class="inspector-btn danger" id="btn-delete-world" title="删除世界及其全部数据">删除世界</button>
            </div>
            <div class="inspector-section">
                <h3 class="panel-title">概览</h3>
                <div class="inspector-overview">
                    <div class="inspector-ov-item"><label>模组</label><span>${this._esc(w.module_name || '无')}</span></div>
                    <div class="inspector-ov-item"><label>实体数</label><span>${this.entities.length}</span></div>
                    <div class="inspector-ov-item"><label>轮次数</label><span>${this.turns.length}</span></div>
                    <div class="inspector-ov-item"><label>记忆条数</label><span id="inspector-memory-count">加载中...</span></div>
                </div>
                <div class="inspector-recap"><label>前情提要</label><p>${this._esc(recapShort)}</p></div>
            </div>
            ${this._renderEntities()}
            ${this._renderTurns()}
            ${this._renderMemories()}`;

        // 状态：事件绑定（删除/记忆检索/回档）必须在 HTML 渲染后挂载
        this.detailEl.querySelector('#btn-delete-world').addEventListener('click', () => this._deleteWorld());
        this._bindMemorySearch();
        this._bindRollbacks();
    }

    _renderEntities() {
        const cards = this.entities
            .filter(e => e.type === 'PC' || e.type === 'NPC')
            .map(e => `
                <div class="inspector-entity-card">
                    <div class="inspector-entity-name">${this._esc(e.name || e.id)} <span class="inspector-entity-type">${e.type}</span></div>
                    <div class="inspector-entity-stats">
                        <span class="stat hp">HP ${e.hp || 0}/${e.hp_max || e.hp || 0}</span>
                        <span class="stat san">SAN ${e.san || 0}/${e.san_max || e.san || 0}</span>
                        <span class="stat mp">MP ${e.mp || 0}/${e.mp_max || e.mp || 0}</span>
                    </div>
                    ${this._renderTags(e.tags)}
                    ${this._renderInventory(e.inventory)}
                </div>`)
            .join('');
        return `
            <div class="inspector-section">
                <h3 class="panel-title">角色 (${this.entities.filter(e => e.type === 'PC' || e.type === 'NPC').length})</h3>
                <div class="inspector-entity-grid">${cards || '<p class="inspector-empty">无角色实体</p>'}</div>
            </div>`;
    }

    _renderTags(tags) {
        if (!tags || tags.length === 0) return '';
        const chips = tags.map(t => `<span class="inspector-tag">${this._esc(t)}</span>`).join('');
        return `<div class="inspector-tags">${chips}</div>`;
    }

    _renderInventory(inventory) {
        if (!inventory || inventory.length === 0) return '';
        const items = inventory.map(i => `<span class="inspector-item">${this._esc(i.name || i)}</span>`).join('');
        return `<div class="inspector-inventory"><label>背包</label> ${items}</div>`;
    }

    _renderTurns() {
        const rows = [...this.turns].reverse().map(t => {
            const ending = t.is_ending ? `<span class="turn-badge ending">${t.ending_type || 'ENDING'}</span>` : '';
            const solid = t.solidified ? ' <span class="turn-badge converged">固</span>' : '';
            return `
                <div class="inspector-turn-row" data-turn="${t.turn_num}">
                    <span class="inspector-turn-num">Turn ${t.turn_num}${ending}${solid}</span>
                    <span class="inspector-turn-text">${this._esc(t.user || '')}</span>
                    <button class="inspector-btn rollback" data-turn="${t.turn_num}">回档至此</button>
                </div>`;
        }).join('');
        return `
            <div class="inspector-section">
                <h3 class="panel-title">轮次 (${this.turns.length})</h3>
                <div class="inspector-turn-list">${rows || '<p class="inspector-empty">无轮次记录</p>'}</div>
            </div>`;
    }

    _renderMemories() {
        return `
            <div class="inspector-section">
                <h3 class="panel-title">记忆检索</h3>
                <div class="inspector-memory-probe">
                    <input type="text" id="memory-query" placeholder="输入语义 query，如：谁收藏了那本书？" value="">
                    <button class="inspector-btn" id="btn-memory-search">检索</button>
                </div>
                <div class="inspector-memory-list"><p class="inspector-empty">记忆加载中...</p></div>
            </div>`;
    }

    // ====================================================================
    // 动作：记忆检索 / 回档 / 删除
    // ====================================================================

    _bindMemorySearch() {
        const btn = this.detailEl.querySelector('#btn-memory-search');
        const input = this.detailEl.querySelector('#memory-query');
        if (!btn || !input) return;
        const doSearch = () => this._loadMemories(input.value);
        btn.addEventListener('click', doSearch);
        input.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') doSearch(); });
    }

    _reRenderMemories() {
        const list = this.detailEl.querySelector('.inspector-memory-list');
        if (!list) return;
        const hits = this.memories.map((m, i) => `
            <div class="inspector-memory-card">
                <div class="inspector-memory-meta">#${i + 1} · 轮次 ${m.turn_num ?? '?'} · 相关度 ${(m.score || 0).toFixed(2)}</div>
                <div class="inspector-memory-text">${this._esc(m.text || '')}</div>
            </div>`).join('');
        list.innerHTML = hits || '<p class="inspector-empty">无命中</p>';
    }

    _bindRollbacks() {
        this.detailEl.querySelectorAll('.inspector-btn.rollback').forEach(btn => {
            btn.addEventListener('click', () => this._rollback(parseInt(btn.dataset.turn, 10)));
        });
    }

    async _rollback(turnNum) {
        if (!confirm(`回档到 Turn ${turnNum} 之前？\n将撤销 Turn ${turnNum} 及之后的所有状态变更与记忆。`)) return;
        this._setStatus(`正在回档到 Turn ${turnNum}...`);
        try {
            const data = await api.post(
                `/api/worlds/${encodeURIComponent(this.currentWorld)}/rollback`,
                { turn_num: turnNum },
            );
            this._setStatus(`回档完成: 删除 ${data.rag_deleted || 0} 条记忆`);
            // 状态：回档后刷新世界 + 轮次（实体状态可能变化）
            await this._selectWorld(this.currentWorld);
        } catch (e) {
            this._setStatus(`回档失败: ${e.message}`, 'error');
        }
    }

    async _deleteWorld() {
        if (!confirm(`确定删除世界 ${this.currentWorld}？\n将清空该世界全部 RAG 记忆与 SQLite 数据，不可恢复！`)) return;
        this._setStatus(`正在删除世界 ${this.currentWorld}...`);
        try {
            const data = await api.del(`/api/worlds/${encodeURIComponent(this.currentWorld)}`);
            this._setStatus(`世界已删除 (清理 ${data.rag_deleted || 0} 条记忆)`);
            this.currentWorld = null;
            this.detailEl.innerHTML = '<p class="placeholder">选择一个世界查看详情</p>';
            await this._loadWorlds();
        } catch (e) {
            this._setStatus(`删除失败: ${e.message}`, 'error');
        }
    }

    // ====================================================================
    // 工具
    // ====================================================================

    _setStatus(msg, level = 'info') {
        if (!this.statusEl) return;
        this.statusEl.textContent = msg;
        this.statusEl.className = 'inspector-status ' + level;
    }

    _esc(text) {
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }
}