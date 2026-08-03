# -*- coding: utf-8 -*-
"""
@File     :   test_llm_client.py
@Desc     :   真实 LLM 客户端逻辑测试：URL 构建、配置错误路径、本地 mock 服务器成功/流式路径
@Note     :   不触真实网络与真实密钥；成功路径用 127.0.0.1 临时 aiohttp 服务模拟提供方，
             配置单例经 monkeypatch 注入指向本地服务
"""

from __future__ import annotations

import json

import aiohttp.web as web
import pytest

import src.core.config as cfg_module
from src.llm.client import _build_api_url, call_llm, call_llm_stream


# ====================================================================
# URL 构建
# ====================================================================


def test_build_api_url_variants():
    cases = {
        "https://api.openai.com/v1": "https://api.openai.com/v1/chat/completions",
        "https://api.openai.com/v1/": "https://api.openai.com/v1/chat/completions",
        "https://api.deepseek.com": "https://api.deepseek.com/v1/chat/completions",
        "https://x.example.com/v1/chat/completions": "https://x.example.com/v1/chat/completions",
        "https://x.example.com/v1/chat/completions/": "https://x.example.com/v1/chat/completions",
    }
    for src, want in cases.items():
        assert _build_api_url(src) == want


# ====================================================================
# 配置错误路径（注入假配置单例，不发起请求）
# ====================================================================


@pytest.fixture
def llm_settings(monkeypatch):
    """注入仅含一个 standard tier + fake provider 的配置单例。"""
    fake = cfg_module.Settings(
        data={
            "model_tiers": {
                "standard": {"provider": "FAKE", "model_name": "fake-model",
                             "temperature": 0.5, "max_tokens": 100},
            }
        },
        providers={"fake": {"base_url": "http://127.0.0.1:1/v1", "api_key": "test-key"}},
    )
    monkeypatch.setattr(cfg_module, "_instance", fake)
    return fake


@pytest.mark.real_llm
async def test_unknown_tier_returns_failure(llm_settings):
    result = await call_llm("nope", [{"role": "user", "content": "hi"}])
    assert not result.is_ok
    assert result.error and "未知的模型层级" in result.error


@pytest.mark.real_llm
async def test_missing_api_key_returns_failure(monkeypatch):
    bad = cfg_module.Settings(
        data={"model_tiers": {"standard": {"provider": "FAKE", "model_name": "m"}}},
        providers={"fake": {"base_url": "http://x/v1"}},  # 缺 api_key
    )
    monkeypatch.setattr(cfg_module, "_instance", bad)
    result = await call_llm("standard", [{"role": "user", "content": "hi"}])
    assert not result.is_ok
    assert result.error and "api_key" in result.error


@pytest.mark.real_llm
async def test_missing_provider_returns_failure(monkeypatch):
    bad = cfg_module.Settings(
        data={"model_tiers": {"standard": {"provider": "GHOST", "model_name": "m"}}},
        providers={},
    )
    monkeypatch.setattr(cfg_module, "_instance", bad)
    result = await call_llm("standard", [{"role": "user", "content": "hi"}])
    assert not result.is_ok
    assert result.error and "未找到提供方" in result.error


# ====================================================================
# 本地 mock 服务器：成功 / 流式 / 4xx 失败
# ====================================================================


@pytest.fixture
async def mock_llm_server(monkeypatch):
    """在临时端口起一个 OpenAI 兼容 mock 服务，并把配置单例指向它。"""

    async def handler(req):
        body = await req.json()
        if body["stream"]:
            resp = web.StreamResponse()
            resp.headers["Content-Type"] = "text/event-stream"
            await resp.prepare(req)
            for piece in ["A", "B"]:
                await resp.write(
                    f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n".encode()
                )
            await resp.write(b"data: [DONE]\n\n")
            return resp
        return web.json_response({"choices": [{"message": {"content": "OK-PAYLOAD"}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    fake = cfg_module.Settings(
        data={
            "model_tiers": {
                "standard": {"provider": "FAKE", "model_name": "fake-model",
                             "temperature": 0.5, "max_tokens": 100},
            }
        },
        providers={"fake": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "test-key"}},
    )
    monkeypatch.setattr(cfg_module, "_instance", fake)
    yield
    await runner.cleanup()


@pytest.mark.real_llm
async def test_call_llm_success_via_mock(mock_llm_server):
    result = await call_llm("standard", [{"role": "user", "content": "hi"}])
    assert result.is_ok
    assert result.text == "OK-PAYLOAD"
    assert result.tier == "standard"
    assert result.model_name == "fake-model"


@pytest.mark.real_llm
async def test_call_llm_stream_via_mock(mock_llm_server):
    chunks = [c async for c in call_llm_stream("standard", [{"role": "user", "content": "hi"}])]
    assert chunks == ["A", "B"]


@pytest.fixture
async def mock_error_server(monkeypatch):
    """mock 服务：固定返回 401，验证 4xx 直接失败不重试。"""

    async def handler(req):
        return web.json_response({"error": "bad key"}, status=401)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    fake = cfg_module.Settings(
        data={"model_tiers": {"standard": {"provider": "FAKE", "model_name": "m"}}},
        providers={"fake": {"base_url": f"http://127.0.0.1:{port}/v1", "api_key": "k"}},
    )
    monkeypatch.setattr(cfg_module, "_instance", fake)
    yield
    await runner.cleanup()


@pytest.mark.real_llm
async def test_call_llm_4xx_fails_without_retry(mock_error_server):
    result = await call_llm("standard", [{"role": "user", "content": "hi"}])
    assert not result.is_ok
    assert result.error == "HTTP 401"
