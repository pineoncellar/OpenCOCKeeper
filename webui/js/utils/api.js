/*- encoding: utf-8 -*/
/*
@File     :   api.js
@Desc     :   REST API 封装 — fetch 统一封装：JSON 解析、错误归一、DELETE/POST 支持
@Note     :   所有 WebUI 后端接口经此访问，错误统一抛 ApiError 便于组件捕获
*/

class ApiError extends Error {
    constructor(message, status, code) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.code = code || '';
    }
}

const api = {
    async request(path, options = {}) {
        const resp = await fetch(path, {
            headers: { 'Content-Type': 'application/json' },
            ...options,
        });
        let data = null;
        try {
            data = await resp.json();
        } catch (e) {
            data = null;
        }
        if (!resp.ok) {
            const err = (data && data.error) || {};
            throw new ApiError(err.message || `HTTP ${resp.status}`, resp.status, err.code);
        }
        return data;
    },

    get(path) {
        return this.request(path);
    },

    post(path, body) {
        return this.request(path, { method: 'POST', body: JSON.stringify(body || {}) });
    },

    del(path) {
        return this.request(path, { method: 'DELETE' });
    },
};