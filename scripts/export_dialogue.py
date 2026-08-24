# -*- coding: utf-8 -*-
"""
@File     :   export_dialogue.py
@Desc     :   读取某世界 trace（logs/traces/<world_id>/turn-*.jsonl），仅提取
             玩家输入(player_input)与守秘人演播(narration)两类事件，按时间戳
             排序渲染成对话记录 md 文档——丢弃 llm_request/tool_* 等过程噪音。
@Note     :   对话锚点：player_input.data.action = 玩家原文，
             narration.data.narration = 守秘人演播文本。
             排序信任 timestamp（发布即真实顺序），不信任 turn_num——历史文件中
             player_input 常被回填为 0，narration 才是真实轮次，故按时间流渲染。
             开场白（Turn 0）由 opening.py 发布 narration 事件，一并导出。
运行: .\.venv\Scripts\python.exe scripts\export_dialogue.py --world-id world_003_module
     不带 --world-id 时列出全部可用世界；--out 指定输出路径，--dry-run 只统计不落盘。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from _common import PROJECT_ROOT, ok, section, warn

TRACE_DIR = PROJECT_ROOT / "logs" / "traces"
_TURN_RE = re.compile(r"^turn-(\d+)\.jsonl$")

# 对话事件白名单：只提取这两类，其余（llm_*/tool_*/converge/directive）全部丢弃
_DIALOGUE_TYPES = {"player_input", "narration"}
_ROLE_LABEL: Dict[str, str] = {"player_input": "玩家", "narration": "守秘人"}


# ---------------- 数据读取 ----------------


def list_worlds() -> List[str]:
    """扫描 traces 根目录，返回含轮次文件的世界 id（字典序）。"""
    if not TRACE_DIR.exists():
        return []
    worlds = []
    for child in sorted(TRACE_DIR.iterdir()):
        if child.is_dir() and any(
            p.is_file() and _TURN_RE.match(p.name) for p in child.iterdir()
        ):
            worlds.append(child.name)
    return worlds


def load_dialogue(world_id: str) -> List[dict]:
    """读取某世界全部轮次文件，抽取 player_input/narration 事件并按时间戳排序。

    返回条目结构：{ts, turn, role, text}；坏行静默跳过，缺正文的事件丢弃。
    """
    d = TRACE_DIR / world_id
    if not d.exists():
        return []
    events = []
    # 状态：轮次文件字典序即轮次序，逐行解析 JSONL
    for p in sorted(d.glob("turn-*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半行写断的坏行跳过
            if evt.get("event_type") not in _DIALOGUE_TYPES:
                continue
            data = evt.get("data") or {}
            if evt["event_type"] == "player_input":
                text = data.get("action")
            else:
                text = data.get("narration")
            if not text or not text.strip():
                continue
            events.append(
                {
                    "ts": evt.get("timestamp", ""),
                    "turn": int(evt.get("turn_num", 0)),
                    "role": evt["event_type"],
                    "text": text.strip(),
                }
            )
    # 状态：以时间戳为序重建真实对话流（同刻保持文件内原序，Python sorted 稳定）
    events.sort(key=lambda e: e["ts"])
    return events


# ---------------- 渲染 ----------------


def render_md(world_id: str, events: List[dict], generated: str) -> str:
    """把对话事件流渲染成 md 文档：角色标题 + 正文，角色切换时换标题。"""
    lines = [f"# 对话记录 — {world_id}", ""]
    lines.append(f"- 世界: `{world_id}`")
    lines.append(f"- 对话条目: {len(events)}")
    lines.append(f"- 生成时间: {generated}")
    lines.append("")
    lines.append("---")
    lines.append("")
    prev_role = None
    for evt in events:
        label = _ROLE_LABEL[evt["role"]]
        # 状态：角色切换才打标题行，同角色连续则仅空行续写
        if prev_role != evt["role"]:
            # 状态：玩家 turn_num 历史常回填为 0，仅守秘人标注真实轮次
            suffix = f" · 第 {evt['turn']} 轮" if evt["role"] == "narration" else ""
            lines.append(f"## {label}{suffix}")
            lines.append("")
        lines.append(evt["text"])
        lines.append("")
        prev_role = evt["role"]
    return "\n".join(lines).rstrip() + "\n"


# ---------------- 入口 ----------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="导出世界 trace 的玩家/AI 对话记录为 md")
    p.add_argument("--world-id", default=None, help="目标世界 id（缺省列出所有可用世界）")
    p.add_argument(
        "--out", default=None,
        help="输出 md 路径（缺省 logs/traces/<world_id>/对话记录.md）",
    )
    p.add_argument("--dry-run", action="store_true", help="只统计对话条目，不写文件")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    worlds = list_worlds()
    if not worlds:
        warn("logs/traces 下没有任何世界 trace")
        return 1

    if not args.world_id:
        section("可用世界")
        for w in worlds:
            print(f"  · {w}")
        print("使用 --world-id 指定世界导出对话记录。")
        return 0

    if args.world_id not in worlds:
        warn(f"世界 {args.world_id} 不存在（可用: {', '.join(worlds)}）")
        return 1

    section(f"导出对话记录 — {args.world_id}")
    events = load_dialogue(args.world_id)
    if not events:
        warn("该世界没有 player_input / narration 事件")
        return 1
    ok(f"提取对话条目 {len(events)} 条")

    if args.dry_run:
        return 0

    out_path = Path(args.out) if args.out else TRACE_DIR / args.world_id / "对话记录.md"
    md = render_md(args.world_id, events, datetime.now().isoformat(timespec="seconds"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    ok(f"已写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
