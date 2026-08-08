# -*- coding: utf-8 -*-
"""
@File     :   search_module_demo.py
@Desc     :   模组检索质量测试脚本：对 data/modules 下的模组做 search_module 查询并打印命中
@Note     :   检索为纯本地（BM25 + 标题加权）；模组导入期由 LLM 划界（见 src/retrieval/structure）；
             支持交互式与 --query 一次性查询；命中展示标题 / 来源 / 得分 / 原文片段
"""

from __future__ import annotations

import argparse

from _common import fail, ok, section, warn

from src.module import ensure_modules_dir, list_modules
from src.retrieval import search_module

# 原文片段展示长度（超长截断）
_SNIPPET_LEN = 160


def main() -> int:
    parser = argparse.ArgumentParser(description="模组检索质量测试")
    parser.add_argument("--module", default=None, help="模组文件名（缺省列出可用模组）")
    parser.add_argument("--query", action="append", default=None,
                        help="一次性查询（可多次指定，逐个打印）")
    parser.add_argument("--top-k", type=int, default=2, help="返回命中数（默认 2）")
    parser.add_argument("--world-id", default=None, help="按世界绑定模组检索（可选）")
    parser.add_argument("--inspect", action="store_true",
                        help="查看模组划界后的章节列表（自检，配合 --module）")
    args = parser.parse_args()

    ensure_modules_dir()
    modules = list_modules()

    if not args.module and not args.world_id:
        if not modules:
            fail("data/modules 下暂无可用模组，请先放入模组文件")
            return 1
        section("可用模组")
        for m in modules:
            ok(f"{m.module_name}  [{m.format}]  {m.size} 字节")
        warn("用 --module <文件名> 指定要检索的模组")
        return 0

    if args.inspect:
        _inspect_module(args)
        return 0

    if args.query:
        for q in args.query:
            _run_query(q, args)
        return 0

    # 交互式 REPL：输入查询回车出结果，空行退出
    section("交互式检索（输入查询回车，空行退出）")
    while True:
        try:
            q = input("查询> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            break
        _run_query(q, args)
    return 0


def _inspect_module(args) -> None:
    """查看模组划界后的章节列表：确认 LLM 划界与切片是否符合预期（放模组后第一步自检）。"""
    if not args.module:
        fail("--inspect 需配合 --module 指定模组文件")
        return
    from src.retrieval import build_index
    try:
        idx = build_index(args.module)
    except Exception as e:  # noqa: BLE001
        fail(f"解析失败: {type(e).__name__}: {e}")
        return
    section(f"章节列表: {args.module}  [{idx.fmt}]  共 {len(idx.sections)} 节")
    for sec in idx.sections:
        print(f"├─ {sec.title}  （{len(sec.content)} 字）")


def _run_query(q: str, args) -> None:
    """执行一次查询并打印命中详情（含标题命中标记与原文片段）。"""
    section(f"查询: {q}")
    try:
        hits = search_module(q, args.module, world_id=args.world_id, top_k=args.top_k)
    except Exception as e:  # noqa: BLE001
        fail(f"检索失败: {type(e).__name__}: {e}")
        return
    if not hits:
        warn("无命中")
        return
    for i, hit in enumerate(hits, start=1):
        sec = hit.section
        tag = "结构命中" if hit.title_match else "全文命中"
        ok(f"#{i} [{tag}] 得分 {hit.score}  来源: {sec.source_location}")
        print(f"    标题: {sec.title}")
        snippet = " ".join(sec.content.split())
        if snippet:
            shown = snippet[:_SNIPPET_LEN]
            print(f"    原文: {shown}{'…' if len(snippet) > _SNIPPET_LEN else ''}")
        else:
            print("    原文: （空）")


if __name__ == "__main__":
    raise SystemExit(main())
