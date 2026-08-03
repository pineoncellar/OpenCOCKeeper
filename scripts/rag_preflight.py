# -*- coding: utf-8 -*-
"""
@File     :   rag_preflight.py
@Desc     :   RAG 前置自检：真实环境逐项检查（mem0ai 依赖 / memory.rag 配置 / 后端构建 / embedding 连通）
@Note     :   对应 src/memory/preflight.py，供固化工序启动前的环境体检；
             存在 FAIL 项即返回非零退出码，不要在环境未就绪时继续跑固化脚本。
运行: .\.venv\Scripts\python.exe scripts\rag_preflight.py [--offline]
"""

from __future__ import annotations

import argparse

from _common import fail, ok, run_async, section, step


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="RAG 环境前置自检")
    p.add_argument(
        "--offline",
        action="store_true",
        help="跳过真实 embedding 网络连通检查（离线开发用）",
    )
    return p


async def main(args) -> int:
    from src.core.config import get_settings
    from src.memory import preflight

    section("RAG 前置自检")
    step("逐项检查中…")
    report = await preflight(get_settings(), check_embedding=not args.offline)

    for c in report.checks:
        mark = {"OK": "OK ", "SKIP": "SKIP", "FAIL": "FAIL"}.get(c.status, c.status)
        print(f"  [{mark}] {c.name}: {c.detail}")

    if report.all_ok:
        ok("全部通过，RAG 环境就绪，可运行 memory_consolidate.py / full_chain.py")
        return 0

    fail(f"存在 {len(report.failed)} 项失败，请先修复环境后重试")
    for c in report.failed:
        print(f"      - {c.name}: {c.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
