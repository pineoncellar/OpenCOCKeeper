/*- encoding: utf-8 -*/
/*
@File     :   config_editor.js
@Desc     :   配置可视化编辑器 — 结构化表单编辑 config.yaml + 原始 YAML 高级视图
@Note     :   表单模式编辑关键字段（模型档位/适配器/固化阈值），保存时把编辑后的
             JS 对象序列化为 YAML 提交后端；原始 YAML 模式提供全文编辑 + 校验。
             配置写入后后端约定"重启后生效"，前端保存成功后提示。
*/

class ConfigEditor {
    constructor(container) {
        this.container = container;
        this.config = {};        // 当前配置 dict（表单模式编辑对象）
        this.statusEl = null;
        this.mode = 'form';      // 'form' | 'yaml'
        this._yamlDirty = false;
    }

    init() {
        this.container.innerHTML = `
            <div class="config-status" id="config-status"></div>
            <div class="config-toolbar">
                <button class="config-btn" id="btn-mode-form">表单模式</button>
                <button class="config-btn" id="btn-mode-yaml">YAML 模式</button>
                <span class="config-spacer"></span>
                <button class="config-btn" id="btn-config-reload">重新加载</button>
                <button class="config-btn danger" id="btn-config-save">保存（重启后生效）</button>
            </div>
            <div class="config-body" id="config-body"></div>`;
        this.statusEl = this.container.querySelector('#config-status');
        this.bodyEl = this.container.querySelector('#config-body');

        this.container.querySelector('#btn-mode-form').addEventListener('click', () => this._setMode('form'));
        this.container.querySelector('#btn-mode-yaml').addEventListener('click', () => this._setMode('yaml'));
        this.container.querySelector('#btn-config-reload').addEventListener('click', () => this._load());
        this.container.querySelector('#btn-config-save').addEventListener('click', () => this._save());

        this._load();
    }

    // ====================================================================
    // 加载
    // ====================================================================

    async _load() {
        this._setStatus('加载配置...');
        try {
            const data = await api.get('/api/config');
            this.config = data.config || {};
            this._renderForm();
            this._setStatus('配置已加载');
        } catch (e) {
            this._setStatus(`配置加载失败: ${e.message}`, 'error');
        }
    }

    // ====================================================================
    // 模式切换
    // ====================================================================

    _setMode(mode) {
        this.mode = mode;
        // 状态：切换按钮激活态
        this.container.querySelectorAll('#btn-mode-form, #btn-mode-yaml').forEach(b => b.classList.remove('active'));
        this.container.querySelector(mode === 'form' ? '#btn-mode-form' : '#btn-mode-yaml').classList.add('active');
        this.mode === 'form' ? this._renderForm() : this._renderYaml();
    }

    // ====================================================================
    // 表单渲染
    // ====================================================================

    _renderForm() {
        const c = this.config;
        const tiers = c.model_tiers || {};
        const mem = c.memory || {};
        const ctx = c.context || {};

        // 状态：模型档位表单（smart/standard/fast 各一行）
        const tierRows = Object.entries(tiers).map(([name, t]) => `
            <div class="config-row tier-row">
                <div class="config-row-label">${this._esc(name)} <small>${t.provider || ''}</small></div>
                <div class="config-row-field">
                    <label>模型</label>
                    <input data-path="model_tiers.${name}.model_name" value="${this._esc(t.model_name || '')}">
                </div>
                <div class="config-row-field">
                    <label>温度</label>
                    <input type="number" step="0.1" min="0" max="2" data-path="model_tiers.${name}.temperature" value="${t.temperature ?? ''}">
                </div>
                <div class="config-row-field">
                    <label>Max Tokens</label>
                    <input type="number" min="1" data-path="model_tiers.${name}.max_tokens" value="${t.max_tokens ?? ''}">
                </div>
            </div>`).join('');

        this.bodyEl.innerHTML = `
            ${this._section('模型档位', tierRows)}

            ${this._section('适配器', `
                <div class="config-row">
                    <div class="config-row-label">激活适配器</div>
                    <div class="config-row-field">
                        <select data-path="adapter.active">
                            ${['cli', 'onebot'].map(a => `<option value="${a}" ${c.adapter && c.adapter.active === a ? 'selected' : ''}>${a}</option>`).join('')}
                        </select>
                    </div>
                </div>
                <div class="config-row">
                    <div class="config-row-label">WebUI 启用</div>
                    <div class="config-row-field">
                        <input type="checkbox" data-path="webui.enabled" ${c.webui && c.webui.enabled ? 'checked' : ''}>
                    </div>
                </div>` )}

            ${this._section('上下文与固化', `
                <div class="config-row">
                    <div class="config-row-label">近程历史轮数</div>
                    <div class="config-row-field">
                        <input type="number" min="1" data-path="context.assembler.recent_turns" value="${ctx.assembler?.recent_turns ?? ''}">
                    </div>
                </div>
                <div class="config-row">
                    <div class="config-row-label">未固化轮数阈值</div>
                    <div class="config-row-field">
                        <input type="number" min="1" data-path="memory.solidify_min_turns" value="${mem.solidify_min_turns ?? ''}">
                    </div>
                </div>
                <div class="config-row">
                    <div class="config-row-label">最小固化间隔(秒)</div>
                    <div class="config-row-field">
                        <input type="number" min="0" data-path="memory.solidify_min_interval" value="${mem.solidify_min_interval ?? ''}">
                    </div>
                </div>`)}`;

        // 状态：绑定所有 data-path 输入框到 this.config（改完即写回对象）
        this.bodyEl.querySelectorAll('[data-path]').forEach(el => {
            const path = el.dataset.path;
            const apply = () => {
                let node = this.config;
                const parts = path.split('.');
                for (let i = 0; i < parts.length - 1; i++) {
                    const p = parts[i];
                    if (typeof node[p] !== 'object' || node[p] === null) node[p] = {};
                    node = node[p];
                }
                const key = parts[parts.length - 1];
                const raw = el.type === 'checkbox' ? el.checked : el.value;
                node[key] = el.type === 'number' ? (raw === '' ? null : Number(raw)) : raw;
            };
            el.addEventListener('input', apply);
            el.addEventListener('change', apply);
        });
    }

    // ====================================================================
    // YAML 模式
    // ====================================================================

    _renderYaml() {
        // 状态：YAML 模式从当前 this.config 序列化（若表单模式改过则带改动）
        const text = this._yamlDump(this.config);
        this.bodyEl.innerHTML = `
            <div class="config-yaml-wrap">
                <textarea id="config-yaml-text" spellcheck="false">${this._escHtml(text)}</textarea>
            </div>
            <div class="config-yaml-actions">
                <button class="config-btn" id="btn-yaml-validate">校验</button>
            </div>`;
        this.container.querySelector('#btn-yaml-validate').addEventListener('click', () => this._validateYaml());
    }

    async _validateYaml() {
        const textarea = this.container.querySelector('#config-yaml-text');
        const text = textarea ? textarea.value : '';
        if (!text.trim()) { this._setStatus('YAML 为空', 'error'); return; }
        this._setStatus('校验中...');
        try {
            await api.post('/api/config/validate', { yaml: text });
            this._setStatus('YAML 校验通过');
        } catch (e) {
            this._setStatus(`校验失败: ${e.message}`, 'error');
        }
    }

    // ====================================================================
    // 保存
    // ====================================================================

    async _save() {
        // 状态：YAML 模式用 textarea 原文，表单模式用序列化的 this.config
        let yamlText;
        if (this.mode === 'yaml') {
            const textarea = this.container.querySelector('#config-yaml-text');
            yamlText = textarea ? textarea.value : '';
            if (!yamlText.trim()) { this._setStatus('YAML 为空，无法保存', 'error'); return; }
        } else {
            yamlText = this._yamlDump(this.config);
        }
        this._setStatus('保存中...');
        try {
            const data = await api.post('/api/config/save', { yaml: yamlText });
            this._setStatus(data.message || '配置已保存');
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

    // ====================================================================
    // 最小 YAML 序列化（够用即可，处理本项目 config 结构）
    // ====================================================================

    _yamlDump(obj) {
        const lines = [];
        const indent = '  ';
        const dumpValue = (value, depth, key) => {
            const pad = indent.repeat(depth);
            if (value === null || value === undefined) return `${pad}${key}: null`;
            if (typeof value === 'object' && Array.isArray(value)) {
                if (value.length === 0) return `${pad}${key}: []`;
                const out = [`${pad}${key}:`];
                value.forEach(v => {
                    if (v && typeof v === 'object') {
                        const sub = this._dumpInline(v, depth + 1);
                        out.push(`${indent.repeat(depth + 1)}- ${sub}`);
                    } else {
                        out.push(`${indent.repeat(depth + 1)}- ${this._scalar(v)}`);
                    }
                });
                return out.join('\n');
            }
            if (typeof value === 'object') {
                const entries = Object.entries(value);
                if (entries.length === 0) return `${pad}${key}: {}`;
                const out = [`${pad}${key}:`];
                // 状态：递归子键；dumpKeyValue 为下方局部 const，调用时已初始化
                entries.forEach(([k, v]) => out.push(dumpKeyValue(v, depth + 1, k)));
                return out.join('\n');
            }
            return `${pad}${key}: ${this._scalar(value)}`;
        };
        const dumpKeyValue = (value, depth, key) => dumpValue(value, depth, key);
        Object.entries(obj).forEach(([k, v]) => lines.push(dumpValue(v, 0, k)));
        return lines.join('\n') + '\n';
    }

    _dumpInline(obj, depth) {
        // 状态：数组内嵌套对象压成单行 {k: v, ...}，避免列表嵌套缩进爆炸
        const parts = Object.entries(obj).map(([k, v]) => `${k}: ${this._scalar(v)}`);
        return `{ ${parts.join(', ')} }`;
    }

    _scalar(v) {
        if (v === null || v === undefined) return 'null';
        if (typeof v === 'boolean') return v ? 'true' : 'false';
        if (typeof v === 'number') return String(v);
        // 状态：含特殊字符的字符串加引号，避免 YAML 解析歧义
        if (/^[\w.\-/]+$/.test(v)) return v;
        return JSON.stringify(v);
    }
}