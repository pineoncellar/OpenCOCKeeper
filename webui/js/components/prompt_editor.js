/*- encoding: utf-8 -*/
/*
@File     :   prompt_editor.js
@Desc     :   提示词编辑器 — 逐条表单编辑 prompts.yaml + 原始 YAML 高级视图，保存即热重载
@Note     :   逐条模式列出现有/内置默认提示词 key（按分区分组），每条一个 textarea，
             空值条目保存时跳过（回退内置默认）；YAML 模式提供全文编辑 + 校验；
             保存走 /api/prompts/save 或 /api/prompts/save_raw，后端写入后立即
             reload_prompts() 热重载——提示词改动运行时即刻生效，无需重启。
*/

// 嵌套 dict → 扁平点号路径 dict（如 {a:{b:1}} → {a.b:1}），供逐条表单回填
function flattenPrompts(obj, prefix = '') {
    const out = {};
    for (const [k, v] of Object.entries(obj || {})) {
        const path = prefix ? `${prefix}.${k}` : k;
        if (v && typeof v === 'object' && !Array.isArray(v)) {
            Object.assign(out, flattenPrompts(v, path));
        } else {
            out[path] = v;
        }
    }
    return out;
}

// 分区名 → 中文标题（仅用于分组展示，不参与存储）
const PROMPT_GROUP_LABELS = {
    director: '主 Agent（Director）',
    narrator: 'Narrator（润色 Agent）',
    opening: 'Opening（开场 Agent）',
    directive: '收尾工具',
    memory: '记忆系统',
    retrieval: '模组结构划界',
    tools: '工具描述',
};

class PromptEditor {
    constructor(container) {
        this.container = container;
        this.keys = [];        // 全部可用 key（文件 + 内置默认，点号路径）
        this.flat = {};        // 文件已配置值的扁平 dict
        this.statusEl = null;
        this.bodyEl = null;
        this.mode = 'form';    // 'form' | 'yaml'
    }

    init() {
        this.container.innerHTML = `
            <div class="config-status" id="prompt-status"></div>
            <div class="config-toolbar">
                <button class="config-btn" id="btn-prompt-mode-form">逐条编辑</button>
                <button class="config-btn" id="btn-prompt-mode-yaml">YAML 模式</button>
                <span class="config-spacer"></span>
                <button class="config-btn" id="btn-prompt-reload">重新加载</button>
                <button class="config-btn" id="btn-prompt-save">保存并热重载</button>
            </div>
            <div class="config-body prompt-body" id="prompt-body"></div>`;
        this.statusEl = this.container.querySelector('#prompt-status');
        this.bodyEl = this.container.querySelector('#prompt-body');

        this.container.querySelector('#btn-prompt-mode-form').addEventListener('click', () => this._setMode('form'));
        this.container.querySelector('#btn-prompt-mode-yaml').addEventListener('click', () => this._setMode('yaml'));
        this.container.querySelector('#btn-prompt-reload').addEventListener('click', () => this._load());
        this.container.querySelector('#btn-prompt-save').addEventListener('click', () => this._save());

        this._load();
    }

    // ====================================================================
    // 加载
    // ====================================================================

    async _load() {
        this._setStatus('加载提示词...');
        try {
            const data = await api.get('/api/prompts');
            this.keys = data.keys || [];
            this.flat = flattenPrompts(data.prompts);
            this._setMode(this.mode, true);
            this._setStatus('提示词已加载（点击保存并热重载即时生效）');
        } catch (e) {
            this._setStatus(`提示词加载失败: ${e.message}`, 'error');
        }
    }

    // ====================================================================
    // 模式切换
    // ====================================================================

    _setMode(mode, force = false) {
        if (mode === this.mode && !force) return;
        this.mode = mode;
        // 状态：切换按钮激活态
        this.container.querySelectorAll('#btn-prompt-mode-form, #btn-prompt-mode-yaml').forEach(b => b.classList.remove('active'));
        this.container.querySelector(mode === 'form' ? '#btn-prompt-mode-form' : '#btn-prompt-mode-yaml').classList.add('active');
        this.mode === 'form' ? this._renderForm() : this._renderYaml();
    }

    // ====================================================================
    // 逐条表单渲染（按分区分组）
    // ====================================================================

    _renderForm() {
        // 状态：按 key 首段分区分组，保持 keys 原有顺序
        const groups = [];
        const groupIndex = {};
        for (const key of this.keys) {
            const group = key.split('.')[0];
            if (!(group in groupIndex)) {
                groupIndex[group] = groups.length;
                groups.push({ group, keys: [] });
            }
            groups[groupIndex[group]].keys.push(key);
        }

        const sections = groups.map(g => {
            const label = PROMPT_GROUP_LABELS[g.group] || g.group;
            const rows = g.keys.map(key => {
                const hasFile = Object.prototype.hasOwnProperty.call(this.flat, key);
                const value = hasFile ? this.flat[key] : '';
                const badge = hasFile ? '' : '<small>(内置默认)</small>';
                return `
                    <div class="prompt-row">
                        <div class="prompt-key">${this._esc(key)} ${badge}</div>
                        <textarea data-key="${this._esc(key)}" spellcheck="false">${this._escHtml(value)}</textarea>
                    </div>`;
            }).join('');
            return this._section(label, rows);
        }).join('');

        this.bodyEl.innerHTML = sections || '<div class="config-status">无可用提示词 key</div>';
    }

    // ====================================================================
    // YAML 模式
    // ====================================================================

    async _renderYaml() {
        this.bodyEl.innerHTML = '<div class="config-status">加载原始 YAML...</div>';
        try {
            const data = await api.get('/api/prompts/raw');
            this.bodyEl.innerHTML = `
                <div class="config-yaml-wrap">
                    <textarea id="prompt-yaml-text" spellcheck="false">${this._escHtml(data.yaml || '')}</textarea>
                </div>
                <div class="config-yaml-actions">
                    <button class="config-btn" id="btn-prompt-yaml-validate">校验</button>
                </div>`;
            this.container.querySelector('#btn-prompt-yaml-validate').addEventListener('click', () => this._validateYaml());
        } catch (e) {
            this._setStatus(`YAML 加载失败: ${e.message}`, 'error');
        }
    }

    async _validateYaml() {
        const textarea = this.container.querySelector('#prompt-yaml-text');
        const text = textarea ? textarea.value : '';
        if (!text.trim()) { this._setStatus('YAML 为空', 'error'); return; }
        this._setStatus('校验中...');
        try {
            await api.post('/api/prompts/validate', { yaml: text });
            this._setStatus('YAML 校验通过');
        } catch (e) {
            this._setStatus(`校验失败: ${e.message}`, 'error');
        }
    }

    // ====================================================================
    // 保存（表单模式提交扁平 dict，YAML 模式提交全文；后端均写入 + 热重载）
    // ====================================================================

    async _save() {
        this._setStatus('保存中...');
        try {
            let message;
            if (this.mode === 'yaml') {
                const textarea = this.container.querySelector('#prompt-yaml-text');
                const text = textarea ? textarea.value : '';
                if (!text.trim()) { this._setStatus('YAML 为空，无法保存', 'error'); return; }
                const data = await api.post('/api/prompts/save_raw', { yaml: text });
                message = data.message;
            } else {
                // 状态：收集所有非空 textarea，空值条目跳过（回退内置默认）
                const flat = {};
                this.container.querySelectorAll('[data-key]').forEach(el => {
                    const v = el.value;
                    if (v && v.trim()) flat[el.dataset.key] = v;
                });
                const data = await api.post('/api/prompts/save', { prompts: flat });
                message = data.message;
            }
            this._setStatus(message || '提示词已保存并热重载');
            // 状态：保存后重读，使"(内置默认)"标记与实际文件状态对齐
            this._load();
        } catch (e) {
            this._setStatus(`保存失败: ${e.message}`, 'error');
        }
    }

    // ====================================================================
    // 工具
    // ====================================================================

    _section(title, inner) {
        return `
            <div class="config-section">
                <h3 class="panel-title">${this._esc(title)}</h3>
                <div class="config-section-body">${inner}</div>
            </div>`;
    }

    _setStatus(msg, level = 'info') {
        if (!this.statusEl) return;
        this.statusEl.textContent = msg;
        this.statusEl.className = 'config-status ' + level;
    }

    _esc(text) {
        if (text === null || text === undefined) return '';
        const div = document.createElement('div');
        div.textContent = String(text);
        return div.innerHTML;
    }

    _escHtml(text) {
        // 状态：textarea 内容需转义 HTML 实体，防 </textarea> 提前闭合
        return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
}
