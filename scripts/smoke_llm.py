# -*- coding: utf-8 -*-
"""
@File     :   smoke_llm.py
@Desc     :   真实 LLM 链路冒烟：非流式 / 流式 / ask 三入口各打一次真实请求
@Note     :   走真实 provider（config.yaml model_tiers + providers.ini），
             需已配置有效 api_key；任一入口失败即返回非零退出码。
运行: .\.venv\Scripts\python.exe scripts\smoke_llm.py [--tier standard]
"""

from __future__ import annotations

import argparse

from _common import add_common_args, fail, ok, run_async, section, step


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="真实 LLM 三入口冒烟测试")
    add_common_args(p)
    p.add_argument("--skip-stream", action="store_true", help="跳过流式入口")
    return p


async def main(args) -> int:
    from src.llm import ask_llm, call_llm, call_llm_stream

    tier = args.tier
    section(f"真实 LLM 冒烟 — tier={tier}")

    # 1) call_llm 非流式
    step("call_llm（非流式）")
    res = await call_llm(
        tier,
        [{"role": "user", "content": "用一句话介绍你自己。"}],
    )
    if not res.is_ok:
        fail(f"call_llm 失败: {res.error}")
        return 1
    ok(f"成功: model={res.model_name}, 文本长度={len(res.text or '')}")
    print(f"      回复: {(res.text or '')[:80]!r}")

    # 2) ask_llm 快捷入口
    step("ask_llm（system + user）")
    res2 = await ask_llm(
        tier,
        "你是《克苏鲁的呼唤》守秘人助手，回复保持简洁。",
        "你好，请用一句话自我介绍。",
    )
    if not res2.is_ok:
        fail(f"ask_llm 失败: {res2.error}")
        return 1
    ok(f"成功: model={res2.model_name}")
    print(f"      回复: {(res2.text or '')[:80]!r}")

    # 3) call_llm_stream 流式
    if not args.skip_stream:
        step("call_llm_stream（流式）")
        chunks: list[str] = []
        async for chunk in call_llm_stream(
            tier, [{"role": "user", "content": "请从一数到五，每句一行。"}]
        ):
            chunks.append(chunk)
        text = "".join(chunks)
        if not text.strip():
            fail("流式返回为空")
            return 1
        ok(f"成功: 收到 {len(chunks)} 个片段, 合计 {len(text)} 字")
        print(f"      拼接结果: {text[:80]!r}")

    print("\n[PASS] 三条 LLM 入口全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_async(main(build_parser().parse_args())))
