# -*- coding: utf-8 -*-
"""
@File     :   test_search_rule.py
@Desc     :   规则库检索测试：Markdown 标题切片、跨文件 BM25 索引构建、典型 CoC 场景查询
             与 search_rule 只读工具挂载（零 state_diff / 零 SQLite）
@Note     :   核心用例用 tmp 规则库（monkeypatch RULES_DIR）保证 hermetic 与基线稳定；
             另含一条针对真实 data/rules/*.md 的守卫用例——规则库未生成时自动跳过不失败
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.retrieval import (
    RuleIndex,
    build_rule_index,
    clear_rule_cache,
    search_rule,
    search_rule_async,
    split_markdown_sections,
)
from src.retrieval import rules as rules_mod

# 代表性规则 Markdown（仿 scripts/build_rules_from_chm.py 生成的干货格式），
# 覆盖典型 CoC 场景关键词：奖励骰 / 贯穿伤害 / 理智检定
RULE_FILES = {
    "战斗.md": (
        "# 战斗\n\n"
        "当战斗发生时，所有的调查员与角色均依照它们的敏捷次序行动。\n\n"
        "## 近战\n\n"
        "攻击者与防御者都需要投掷百分骰（1D100）并比较他们结果的成功等级。\n\n"
        "## 极限伤害\n\n"
        "成功等级达到极限的攻击者将能造成额外的伤害：\n\n"
        "  - 钝击武器将造成最大伤害，并附带伤害加值（DB，如果有的话）。\n"
        "  - 贯穿武器（刀刃与子弹）将造成最大伤害，并附带伤害加值并额外投掷一次武器伤害骰。\n\n"
        "## 射击规则\n\n"
        "如果在同一轮中使用手枪射击 2 或 3 次，每一次射击都需遭受一枚惩罚骰。\n"
        "如果在近距开火，射击者在这次技能检定上获得一枚奖励骰。\n"
    ),
    "理智（SAN）.md": (
        "# 理智（SAN）\n\n"
        "当调查员遭遇了来自克苏鲁神话的恐怖之物或目击骇人的事物时，需要以其现有的理智值为目标掷一次百分骰。\n\n"
        "如果你掷出的数值高于现有的理智值，那么你没有通过理智检定，并需要为此损失数目较多的理智值。\n\n"
        "如果一名调查员在单次理智检定中损失了 5 点以上的理智值，他将承受一次严重的心理创伤。\n"
    ),
    "创建一位调查员.md": (
        "# 创建一位调查员\n\n"
        "要进行《克苏鲁的呼唤》游戏，你需要创建一名调查员角色。\n\n"
        "## 第一步：调查员属性\n\n"
        "《克苏鲁的呼唤》中的角色拥有八项属性：力量、体质、意志、敏捷、外貌、体型、智力与教育。\n"
    ),
}


@pytest.fixture
def rules_dir(tmp_path, monkeypatch):
    """临时规则库目录：写入代表性规则 Markdown 并替换 rules.RULES_DIR。"""
    d = tmp_path / "rules"
    d.mkdir()
    for name, text in RULE_FILES.items():
        (d / name).write_text(text, encoding="utf-8")
    monkeypatch.setattr(rules_mod, "RULES_DIR", d)
    clear_rule_cache()
    yield d
    clear_rule_cache()


# ====================================================================
# Markdown 标题切片
# ====================================================================


def test_split_markdown_sections_keeps_headings(rules_dir):
    sections = split_markdown_sections(RULE_FILES["战斗.md"], "战斗.md")
    titles = [s.title for s in sections]
    assert titles == ["战斗", "近战", "极限伤害", "射击规则"]
    # 每节 content 为物理切片，含标题行与正文
    extreme = sections[2]
    assert extreme.content.startswith("## 极限伤害")
    assert "贯穿武器" in extreme.content
    # 拼接回原文，验证 100% 物理对齐（strip 后逐节首尾相连）
    assert "".join(s.content for s in sections).replace("\n", "") == \
        RULE_FILES["战斗.md"].replace("\n", "")


def test_split_markdown_sections_no_heading_falls_back():
    sections = split_markdown_sections("没有任何标题的纯正文。", "x.md")
    assert len(sections) == 1
    assert sections[0].title == "全文"


# ====================================================================
# 索引构建
# ====================================================================


def test_build_rule_index_from_markdown(rules_dir):
    idx = build_rule_index()
    assert isinstance(idx, RuleIndex)
    assert len(idx.sections) == 7  # 战斗 4 节 + 理智 1 节 + 创建调查员 2 节
    titles = {s.title for s in idx.sections}
    assert {"战斗", "近战", "极限伤害", "理智（SAN）", "创建一位调查员"} <= titles
    # 物理定位 = 规则文件名
    sources = {s.source_location for s in idx.sections}
    assert "战斗.md" in sources and "理智（SAN）.md" in sources


def test_build_rule_index_uses_cache(rules_dir):
    idx1 = build_rule_index()
    idx2 = build_rule_index()
    assert idx1 is idx2  # 指纹未变，命中缓存同一对象


def test_build_rule_index_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(rules_mod, "RULES_DIR", tmp_path / "no_such_rules")
    clear_rule_cache()
    idx = build_rule_index()
    assert idx.sections == []


# ====================================================================
# 典型 CoC 场景查询
# ====================================================================


def test_search_rule_bonus_die(rules_dir):
    hits = search_rule("奖励骰", top_k=3)
    assert hits
    assert any("奖励骰" in h.section.content for h in hits)


def test_search_rule_penetration_damage(rules_dir):
    hits = search_rule("贯穿伤害", top_k=3)
    assert hits
    top = hits[0]
    assert "贯穿" in top.section.content
    assert "伤害" in top.section.content


def test_search_rule_sanity_check_failure(rules_dir):
    hits = search_rule("理智检定失败", top_k=3)
    assert hits
    assert any("理智检定" in h.section.content for h in hits)


def test_search_rule_top_k_limits_and_sorts(rules_dir):
    hits = search_rule("战斗", top_k=2)
    assert len(hits) <= 2
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


async def test_search_rule_async_matches_sync(rules_dir):
    sync_hits = search_rule("贯穿伤害", top_k=3)
    async_hits = await search_rule_async("贯穿伤害", top_k=3)
    assert [h.section.content for h in sync_hits] == [
        h.section.content for h in async_hits
    ]


def test_search_rule_no_rules_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(rules_mod, "RULES_DIR", tmp_path / "no_such_rules")
    clear_rule_cache()
    assert search_rule("奖励骰") == []


# ====================================================================
# search_rule 只读工具挂载（ToolRunner / Schema）
# ====================================================================


def test_search_rule_schema_registered():
    from src.agent.schemas import build_tool_schemas

    schemas = build_tool_schemas()
    sr = next(
        s for s in schemas if s["function"]["name"] == "search_rule"
    )
    params = sr["function"]["parameters"]
    assert sr["function"]["description"]
    assert "query" in params["properties"]
    assert params["required"] == ["query"]
    # 只读工具：schema 无注入字段可填
    assert "world_id" not in params["properties"]


async def test_runner_search_rule_readonly_no_diff(storage, world_id, rules_dir):
    """ToolRunner 挂载的 search_rule 只读执行：返回规则原文，不产 state_diff。"""
    from src.agent.loop import build_default_runner

    runner = build_default_runner(storage)
    assert "search_rule" in runner.names()
    runner.reset_diffs()
    runner.reset_checks()
    out = await runner.execute(
        "search_rule", {"query": "贯穿伤害"}, world_id=world_id, turn_num=1
    )
    assert out["ok"] is True
    assert out["hits"]
    hit = out["hits"][0]
    assert hit["source"]  # 规则文件名
    assert hit["title"]
    assert "贯穿" in hit["content"]
    assert "state_diff" not in out
    assert runner.collected_diffs == []  # 只读工具不产 diff


# ====================================================================
# 真实生成规则库（守卫：未生成则跳过，保证基线不因缺数据失败）
# ====================================================================

REAL_RULES_DIR = Path(__file__).resolve().parent.parent / "data" / "rules"


@pytest.mark.skipif(
    not (REAL_RULES_DIR / "战斗.md").is_file()
    or not (REAL_RULES_DIR / "理智（SAN）.md").is_file(),
    reason="真实 data/rules 规则库未生成（先运行 scripts/build_rules_from_chm.py）",
)
def test_search_rule_on_generated_data_rules():
    """从真实生成的 data/rules/*.md 构建索引并查询，验证离线产物可直接被检索。"""
    assert rules_mod.RULES_DIR == REAL_RULES_DIR
    clear_rule_cache()
    idx = build_rule_index()
    assert len(idx.sections) >= 10
    for query, keyword in [
        ("奖励骰", "奖励骰"),
        ("贯穿伤害", "贯穿"),
        ("理智检定失败", "理智检定"),
    ]:
        hits = search_rule(query, top_k=3)
        assert hits, f"查询 {query!r} 在真实规则库上无命中"
        assert any(keyword in h.section.content for h in hits)
