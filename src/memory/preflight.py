# -*- coding: utf-8 -*-
"""
@File     :   preflight.py
@Desc     :   RAG 模块前置自检：依赖 / 配置 / 后端构建 / embedding 连通性逐项检查
@Note     :   供事件处理流启动时调用；任何 FAIL 项都应中止任务等待环境修复后重试，
             而非降级继续（与固化接口失败即抛错的原则一致）
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

from ..core.config import ConfigError, Settings, get_settings
from ..core.log import get_logger
from .backend import Mem0Memory

logger = get_logger(__name__)

STATUS_OK = "OK"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"


# ============================================
# 结果类型
# ============================================


@dataclass
class PreflightCheck:
    """单条自检结果：检查项名 + 状态 + 补充说明。"""
    name: str
    status: str
    detail: str = ""


@dataclass
class PreflightReport:
    """自检报告：逐条结果 + 是否全部通过 + 失败项列表。"""
    checks: List[PreflightCheck] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        """全部检查项通过才视为就绪。"""
        return all(c.status == STATUS_OK for c in self.checks)

    @property
    def failed(self) -> List[PreflightCheck]:
        """返回失败项，供上层打印/告警。"""
        return [c for c in self.checks if c.status == STATUS_FAIL]

    def brief(self) -> dict:
        """轻量摘要，供日志输出。"""
        return {
            "all_ok": self.all_ok,
            "checks": [
                {"name": c.name, "status": c.status, "detail": c.detail}
                for c in self.checks
            ],
        }


# ============================================
# 自检主流程
# ============================================


async def preflight(
    settings: Optional[Settings] = None,
    *,
    check_embedding: bool = True,
) -> PreflightReport:
    """执行 RAG 前置自检，返回逐项报告。

    检查项：mem0ai 依赖、memory.rag 配置、Mem0 后端构建、embedding 端点连通（可选）。
    check_embedding=False 跳过真实网络请求，适配离线开发场景。
    """
    settings = settings or get_settings()
    report = PreflightReport()

    # 依赖检查：mem0ai 是否可导入
    try:
        import mem0  # noqa: F401

        report.checks.append(
            PreflightCheck("依赖 mem0ai", STATUS_OK, f"已安装 v{mem0.__version__}")
        )
    except ImportError:
        report.checks.append(
            PreflightCheck(
                "依赖 mem0ai",
                STATUS_FAIL,
                "未安装，请运行: uv add mem0ai",
            )
        )
        return report  # 状态：依赖缺失，后续检查无意义

    # 配置检查：memory.rag 段与提供方密钥
    rag = settings.get("memory.rag") or {}
    embedder_model = rag.get("embedder_model")
    embedder_provider = rag.get("embedder_provider")
    if not embedder_model or not embedder_provider:
        report.checks.append(
            PreflightCheck(
                "配置 memory.rag",
                STATUS_FAIL,
                "缺少 embedder_model / embedder_provider，请在 config.yaml 补齐",
            )
        )
    else:
        prov = settings.get_provider(str(embedder_provider))
        if not prov or not prov.get("api_key"):
            report.checks.append(
                PreflightCheck(
                    "配置 memory.rag",
                    STATUS_FAIL,
                    f"提供方 '{embedder_provider}' 未在 providers.ini 配置 api_key",
                )
            )
        else:
            report.checks.append(
                PreflightCheck(
                    "配置 memory.rag",
                    STATUS_OK,
                    f"embedder={embedder_model} provider={embedder_provider}",
                )
            )

    # 构建检查：从配置实例化真实后端组件（不触网络）
    try:
        backend = Mem0Memory.from_config(settings)
        backend.ensure_ready()
        report.checks.append(
            PreflightCheck("Mem0 后端构建", STATUS_OK, "组件构建成功")
        )
    except Exception as e:  # noqa: BLE001
        report.checks.append(
            PreflightCheck("Mem0 后端构建", STATUS_FAIL, str(e))
        )

    # 连通检查：向嵌入端点发一次最小请求（可选）
    if not check_embedding:
        report.checks.append(
            PreflightCheck("embedding 端点", STATUS_SKIP, "已跳过（check_embedding=False）")
        )
        return report
    try:
        dims = await _probe_embedding(settings)
        report.checks.append(
            PreflightCheck("embedding 端点", STATUS_OK, f"连通正常，向量维度 {dims}")
        )
    except Exception as e:  # noqa: BLE001
        report.checks.append(
            PreflightCheck("embedding 端点", STATUS_FAIL, str(e))
        )

    return report


async def _probe_embedding(settings: Settings) -> int:
    """向嵌入提供方发一次最小请求，返回向量维度；失败抛异常由上层转 FAIL。"""
    rag = settings.get("memory.rag") or {}
    provider = str(rag.get("embedder_provider", ""))
    model = str(rag.get("embedder_model", ""))
    prov = settings.get_provider(provider)
    if not prov:
        raise ConfigError(f"嵌入提供方 '{provider}' 未配置")
    url = prov.get("base_url", "").rstrip("/") + "/embeddings"
    body = json.dumps({"model": model, "input": ["自检"]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {prov.get('api_key', '')}",
        },
    )

    # 同步阻塞式调用放线程池，避免卡住事件循环  # 状态：网络探测
    def _do() -> dict:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    data = await asyncio.get_running_loop().run_in_executor(None, _do)
    vector = data["data"][0]["embedding"]
    return len(vector)
