# -*- coding: utf-8 -*-
"""
@File     :   combat.py
@Desc     :   战斗裁判子代理（Combat Agent）+ 战斗轮编排——专注 CoC 7th 战斗流转，
             与主 Agent（Director）线性平行：in_combat 为真时由 pipeline 分流到本模块，
             下游仍产出 NarrativeDirective 交 Narrator 演播
@Note     :   软硬两分——战场切片（turn_order/cursor/pending_reaction/outnumbered）走
             world_state.combat_runtime 软状态，绝不进 state_diff 参与回档逆向；
             物理真相（HP/伤情 Tag）经 combat_resolve 产 state_diff 落库，回档后依
             存活实体由本模块重放收敛；所有骰点/伤害/重伤由 src.rules.combat 内核结算，
             本模块只做实体读取、上下文装配、契约提取与状态推进
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.core.config import get_settings
from src.core.exceptions import CombatError
from src.core.log import get_logger
from src.core.prompts import get_prompt
from src.rules import Combatant, dexterity_order
from src.agent.directive import NarrativeDirective
from src.agent.loop import ToolRunner, run_tool_loop
from src.agent.narrator import Narrator
from src.tools.commit import apply_turn_change
from src.webui.trace_engine import (
    get_trace_bus,
    make_directive_event,
    make_narration_event,
)

logger = get_logger(__name__)

# 默认战斗裁决模型档位（可被 config.context.combat / 构造参数覆盖）
DEFAULT_TIER = "smart"

# 单实体战斗裁决的工具轮次预算：每个行动者独立闭环，数值由 config.context.combat 覆盖
DEFAULT_MAX_ITERATIONS = 6

# 收尾工具名：Combat Agent 裁决完毕调用即交卷，闭环据此提前收敛
PRESENT_COMBAT_NAME = "present_combat"

# 无行动能力的持久状态 Tag（回档保留，不随战斗结束清除）
_DISABLED_TAGS = frozenset({"昏迷", "濒死"})

# 参战 NPC 入场标记：实体带此 Tag 视为战斗参战者（由 start_combat 调用方保证）
_BATTLE_TAG = "战斗中"

# 判定无行动能力要读的战斗属性键（英文缩写优先，中文兜底）
_DEX_KEYS = ("DEX", "敏捷")
_FIGHT_KEYS = ("斗殴", "格斗(斗殴)", "格斗")
_SHOOT_KEYS = ("射击(手枪)", "手枪", "步枪", "射击")


# ============================================
# combat_runtime 软状态：战场切片生命周期
# ============================================


def read_combat(storage, world_id: str) -> dict:
    """读取战斗运行时软状态（缺省补全字段，保证调用方零防御）。"""
    cc = storage.get_combat_runtime(world_id)
    cc.setdefault("in_combat", False)
    cc.setdefault("round_num", 1)
    cc.setdefault("turn_order", [])
    cc.setdefault("cursor", 0)
    cc.setdefault("pending_reaction", None)
    cc.setdefault("outnumbered", {})
    return cc


def save_combat(storage, world_id: str, cc: dict) -> None:
    """整体写回战斗运行时软状态（Combat Agent 的唯一落盘口）。"""
    storage.set_combat_runtime(world_id, cc)


def _extract(entity: dict, keys: tuple, default: int = 0) -> int:
    """从实体属性表按候选键取首个整数值；全部缺失返回 default。"""
    skills = entity.get("attributes_and_skills") or {}
    for k in keys:
        v = skills.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return default


def _combatant(entity: dict, ready: bool) -> Combatant:
    """从实体面板构造先攻要素：DEX + 近战/远程技能值。"""
    skills = entity.get("attributes_and_skills") or {}
    return Combatant(
        entity_id=entity["id"],
        dex=_extract(entity, _DEX_KEYS),
        fight_skill=_extract(entity, _FIGHT_KEYS),
        shoot_skill=_extract(entity, _SHOOT_KEYS),
        ready_gun=ready,
    )


def start_combat(
    storage,
    world_id: str,
    participant_ids: List[str],
    *,
    ranged: bool = False,
    ready_gun_ids: Optional[List[str]] = None,
) -> dict:
    """初始化战斗并写入软状态：读取面板 → DEX 排序 → 写 combat_runtime。

    participant_ids 为参战者实体 ID 列表（PC+NPC）；ready_gun_ids 命中者视为拔枪待击
    （DEX+50）；实体会被挂上"战斗中"标记（经 update_entity 的 tags 直接写软标记，
    不进 state_diff——战斗结束统一清理）。返回写好的战场切片。
    """
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    missing = [eid for eid in participant_ids if eid not in entities]
    if missing:
        raise CombatError(f"参战者不存在: {missing}")
    ready_set = set(ready_gun_ids or [])
    combatants = [
        _combatant(entities[eid], eid in ready_set) for eid in participant_ids
    ]
    order = dexterity_order(combatants, ranged=ranged)
    turn_order = [
        {
            "entity_id": entry.entity_id,
            "dex": entry.dex,
            "effective_dex": entry.effective_dex,
            "ready_gun": entry.entity_id in ready_set,
        }
        for entry in order
    ]
    cc = {
        "in_combat": True,
        "round_num": 1,
        "turn_order": turn_order,
        "cursor": 0,
        "pending_reaction": None,
        "outnumbered": {},
    }
    save_combat(storage, world_id, cc)
    # 状态：入场临时标记（战斗专属、非持久伤情），脱战时统一卸下
    for eid in participant_ids:
        tags = list(entities[eid].get("tags") or [])
        if _BATTLE_TAG not in tags:
            storage.update_entity(world_id, eid, tags=tags + [_BATTLE_TAG])
    return cc


def end_combat(storage, world_id: str) -> None:
    """脱战收束：注销战斗软状态并卸下参战者的"战斗中"临时标记。

    持久伤情 Tag（重伤/昏迷/濒死）保留在实体上，由后续急救/搜刮剧情自然处理。
    """
    cc = read_combat(storage, world_id)
    for entry in cc.get("turn_order") or []:
        eid = entry["entity_id"]
        entity = storage.get_entity(world_id, eid)
        if entity is None:
            continue
        tags = [t for t in (entity.get("tags") or []) if t != _BATTLE_TAG]
        storage.update_entity(world_id, eid, tags=tags)
    save_combat(storage, world_id, {})


def render_combat_order(storage, world_id: str, cc: dict) -> str:
    """渲染先攻顺序为可读文本（序号 + 实体名 + DEX），供工具回填与战斗开场公告。"""
    names = {e["id"]: (e.get("name") or e["id"]) for e in storage.get_entities(world_id)}
    lines: List[str] = []
    for i, entry in enumerate(cc.get("turn_order") or [], start=1):
        eid = entry["entity_id"]
        dex = entry.get("effective_dex")
        try:
            dex_text = str(int(dex))
        except (TypeError, ValueError):
            dex_text = str(dex)
        mark = "（拔枪待击）" if entry.get("ready_gun") else ""
        lines.append(f"{i}. {names.get(eid, eid)}（DEX {dex_text}）{mark}")
    return "\n".join(lines)


def render_combat_intro(storage, world_id: str, cc: dict) -> str:
    """渲染战斗开场公告：正式分界线 + 先攻顺序，供 pipeline 程序拼接在叙事之前。

    状态：纯程序拼装，绝不注入 Narrator 上下文（避免排版文本引发模型模仿与格式污染）。
    """
    banner = (get_prompt("combat.intro_banner") or "").strip()
    title = (get_prompt("combat.intro_order_title") or "").strip()
    order = render_combat_order(storage, world_id, cc)
    if not order:
        return banner
    return f"{banner}\n\n{title}\n{order}"


def render_turn_banner(
    storage, world_id: str, cc: dict, current: str, entities: Dict[str, dict]
) -> str:
    """渲染单步行动横幅：大轮次 + 当前行动主体 + 先攻次序（含生死/昏迷简况）。

    纯粹由 combat_runtime 软状态拼装，零 LLM、零 Token；
    状态：只在 Narrator 产出之后由程序拼接，绝不进入 Narrator 上下文。
    """
    names = {eid: (e.get("name") or eid) for eid, e in entities.items()}
    turn_tpl = get_prompt("combat.banner_turn")
    order_tpl = get_prompt("combat.banner_order")
    active_mark = get_prompt("combat.banner_state_active")
    down_mark = get_prompt("combat.banner_state_down")
    head = turn_tpl.format(
        round=cc.get("round_num"), actor=names.get(current, current)
    )
    seq: List[str] = []
    for i, entry in enumerate(cc.get("turn_order") or [], start=1):
        eid = entry["entity_id"]
        entity = entities.get(eid)
        mark = active_mark if eid == current else ""
        if entity is not None and _incapacitated(entity):
            mark = down_mark
        label = names.get(eid, eid)
        seq.append(f"{i}.{label}（{mark}）" if mark else f"{i}.{label}")
    lines = [head]
    if seq:
        lines.append(order_tpl.format(order=" → ".join(seq)))
    return "\n".join(lines)


def _incapacitated(entity: Optional[dict]) -> bool:
    """是否失去行动能力：HP 归零或带持久失去行动 Tag。"""
    if entity is None:
        return True
    if int(entity.get("hp") or 0) <= 0:
        return True
    return bool(_DISABLED_TAGS & set(entity.get("tags") or []))


def current_actor(cc: dict, entities: Dict[str, dict]) -> Optional[str]:
    """取当前游标所指（或其最近后继）的可行动者；游标原地对齐，不步进。

    用于判定"本回合该谁行动"；回合结算完毕的后继推进请用 advance_battlefield。
    全员无行动能力返回 None（触发脱战判定）。
    """
    order = cc.get("turn_order") or []
    if not order:
        return None
    cursor = int(cc.get("cursor") or 0)
    # 状态：从游标处沿顺位找首个可行动者，兼容轮间被弄晕/打死导致当前位失效
    for offset in range(len(order)):
        idx = (cursor + offset) % len(order)
        if not _incapacitated(entities.get(order[idx]["entity_id"])):
            cc["cursor"] = idx
            return order[idx]["entity_id"]
    return None


def count_outnumbered(cc: dict, entities: Dict[str, dict]) -> Dict[str, int]:
    """统计每个 PC 当前面对的可行动敌对 NPC 数量（围攻人数），供战场面板呈现。

    状态：只做计数不做检定加骰——data/rules 未收录围攻可选规则，加骰口径待定；
    先给 Combat Agent 可见事实，由其在 combat_resolve 的 bonus_penalty_dice 自行裁定。
    """
    order = cc.get("turn_order") or []
    pc_ids: List[str] = []
    npc_count = 0
    for entry in order:
        entity = entities.get(entry["entity_id"])
        if entity is None or _incapacitated(entity):
            continue
        if entity.get("type") == "PC":
            pc_ids.append(entity["id"])
        else:
            npc_count += 1
    # 状态：多 PC 同屏时无法判定各自被谁盯上，统一按战场敌人总数呈现
    return {pid: npc_count for pid in pc_ids}


def advance_battlefield(cc: dict, entities: Dict[str, dict]) -> Optional[str]:
    """把游标步进到下一顺位可行动者（跳过昏迷/死亡）；跨过末位回卷则大轮 +1 并清零围攻计数。

    返回步进后的当前行动者实体 ID；整圈无可行动者返回 None（触发脱战判定）。
    状态：软状态原地推进——不落 diff，回档不逆向。
    """
    order = cc.get("turn_order") or []
    if not order:
        return None
    cursor = int(cc.get("cursor") or 0)
    round_num = int(cc.get("round_num") or 1)
    outnumbered = cc.get("outnumbered") or {}
    # 状态：最多步进一圈——先 +1 再看，走完末位即回卷大轮；
    # 全失能时不写回 cc，避免空转把大轮号平白抬高
    for _ in range(len(order)):
        cursor += 1
        if cursor >= len(order):
            round_num += 1
            cursor = 0
            outnumbered = {}
        entry = order[cursor]
        if not _incapacitated(entities.get(entry["entity_id"])):
            cc["cursor"], cc["round_num"], cc["outnumbered"] = cursor, round_num, outnumbered
            return entry["entity_id"]
    return None


def opponents_remain(cc: dict, entities: Dict[str, dict]) -> bool:
    """对战双方是否仍有对抗（PC 与非 PC 各至少一具可行动者）。

    一方（PC 或 NPC）全部倒下/昏迷即视为战斗失去对抗意义，触发脱战收敛。
    """
    decax = {"pc": 0, "npc": 0}
    for entry in cc.get("turn_order") or []:
        entity = entities.get(entry["entity_id"])
        if entity is None or _incapacitated(entity):
            continue
        kind = "pc" if entity.get("type") == "PC" else "npc"
        decax[kind] += 1
    return decax["pc"] > 0 and decax["npc"] > 0


def _stat_display(entity: dict) -> str:
    """渲染单具实体的战斗面板行（HP/行动状态/关键技能/武器）。"""
    skills = entity.get("attributes_and_skills") or {}
    hp = int(entity.get("hp") or 0)
    hp_max = int(entity.get("hp_max") or 0)
    tags = list(entity.get("tags") or [])
    weapon = ""
    for item in (entity.get("inventory") or []):
        if isinstance(item, dict) and item.get("name"):
            weapon = item["name"]
            break
    parts = [
        f"{entity.get('name')}({entity['id']}) HP {hp}/{hp_max}",
    ]
    if tags:
        parts.append("状态:" + "/".join(tags))
    skills_line = "，".join(
        f"{k}:{v}" for k, v in skills.items() if k in _FIGHT_KEYS or k in _SHOOT_KEYS)
    if skills_line:
        parts.append(skills_line)
    if weapon:
        parts.append(f"持{weapon}")
    return " | ".join(parts)


# ============================================
# 上下文装配
# ============================================


def build_combat_messages(
    storage,
    world_id: str,
    cc: dict,
    action: str,
    current: str,
    entities: Dict[str, dict],
) -> List[dict]:
    """装配 Combat Agent 的 system + user 消息（战场报告 + 本轮上下文）。"""
    order_lines = []
    for entry in cc.get("turn_order") or []:
        entity = entities.get(entry["entity_id"])
        mark = "（行动）" if entry["entity_id"] == current else ""
        dex = entry.get("effective_dex")
        order_lines.append(f"- {entry['entity_id']} 先攻 {dex}{mark}")
    lines = [
        f"战斗轮 {cc.get('round_num')}，当前行动者: {current}。",
        "【顺位】",
        "\n".join(order_lines) or "（无）",
        "【战场面板】",
    ]
    for entry in cc.get("turn_order") or []:
        entity = entities.get(entry["entity_id"])
        if entity is not None:
            lines.append(_stat_display(entity))
    # 状态：围攻态势呈现（只报事实不下惩罚），供 Combat Agent 裁定奖惩骰
    outnumbered = cc.get("outnumbered") or {}
    if outnumbered:
        lines.append("【围攻态势】" + "；".join(
            f"{eid} 面对 {n} 名敌人" for eid, n in outnumbered.items()
        ))
    pending = cc.get("pending_reaction") or {}
    if pending:
        # 状态：恢复结算轮——挂起时未投攻击检定，双方检定在本轮一起掷出
        lines.extend([
            "【挂起反应·恢复结算轮】",
            f"{pending.get('attacker_id')} 已向 {pending.get('target_id')} 发起攻击"
            f"（{pending.get('attack_skill')}，伤害 {pending.get('damage_expression')}），"
            f"本轮等待 {pending.get('target_id')} 声明防御（闪避/反击/硬吃）。",
            "攻击检定尚未投出——双方将一起掷骰：你只需判断玩家声明，"
            "调用一次 combat_resolve（action=strike，attacker/target/技能/伤害表达式沿用挂起数据，"
            "只需给出 defense）由系统同时结算攻击与防御检定、对抗与伤害。",
        ])
        if action:
            lines.append("【本轮玩家行动】" + action)
    else:
        # 状态：按行动者身份分流——PC 回合带玩家输入，NPC 回合为自主行动无输入
        current_entity = entities.get(current) or {}
        if current_entity.get("type") == "PC":
            lines.append("【本轮玩家行动】" + action)
        else:
            lines.append(
                f"【本轮行动者】{current_entity.get('name') or current}（NPC）自主行动，"
                "本轮无玩家输入，请依战场态势为该 NPC 选择战术并结算。"
            )
            lines.append(
                "若该 NPC 向 PC 发起攻击，必须调用 combat_resolve 的 "
                "suspend（只投攻击检定、暂不扣血）把防御决定交还玩家，"
                "严禁替玩家代投闪避/反击。"
            )
    lines.append("请按手记契约交卷；有挂起时在手记末尾向玩家抛出防御选择。")
    return [
        {"role": "system", "content": get_prompt("combat.system")},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ============================================
# Combat Agent 专属 runner 与 schema
# ============================================


def build_combat_runner(
    storage,
    *,
    rng: Optional[object] = None,
    pending: Optional[dict] = None,
    current: Optional[str] = None,
) -> ToolRunner:
    """构造战斗专属 ToolRunner：只暴露 5 工具。

    工具集 = search_rule（规则兜底）+ check_and_update_stats（杂项检定）+
    combat_resolve（对抗/伤害/战技）+ manage_tags（状态标签）+
    present_combat（交卷收尾）；不含模组检索与长程记忆，聚焦战斗流转降低 Token。

    current 为本轮行动者实体 ID——用于行动者白名单（防越权代打与一轮多动）；
    pending 非空表示本轮为挂起恢复轮：把挂起攻击预置到 runner 供注入，
    白名单初始为挂起攻击方（挂起阶段不投检定，双方检定在恢复轮一起掷出）。
    """
    from src.tools.check_and_update_stats import check_and_update_stats as _stats
    from src.tools.combat_resolve import combat_resolve as _resolve
    from src.tools.manage_tags import manage_tags as _tags
    from src.retrieval import search_rule_async as _search_rule

    runner = ToolRunner()
    runner.pending_attack = dict(pending) if pending else None
    # 状态：行动者白名单——恢复轮仅挂起攻击方可出手，常规轮仅当前行动者
    runner.actor_pool = (
        {pending.get("attacker_id")} if pending else ({current} if current else set())
    )

    async def _run_search_rule_tool(**kwargs: Any) -> dict:
        """search_rule：固定检索 data/rules 规则库，返回未加工规则原文（只读零副作用）。"""
        hits = await _search_rule(
            kwargs.get("query") or "",
            top_k=int(kwargs.get("top_k") or 3),
        )
        return {
            "ok": True,
            "hits": [
                {
                    "source": h.section.source_location,
                    "title": h.section.title,
                    "content": h.section.content,
                    "score": h.score,
                }
                for h in hits
            ],
        }

    def _run_stats(**kwargs: Any) -> dict:
        """check_and_update_stats：杂项行动检定（撬锁/机关等），diff 由 runner 抽走。"""
        result = _stats(storage, kwargs, rng=rng)
        check = result["check"]
        if check is not None:
            check = {**check, "entity_id": kwargs.get("entity_id")}
        for extra in result.get("extra_checks") or []:
            runner.collected_checks.append({**extra, "entity_id": kwargs.get("entity_id")})
        return {
            "ok": result["ok"],
            "summary": result["summary_for_agent"],
            "check": check,
            "stats_changed": result["stats_changed"],
            "insanity": result["insanity"],
            "suggested_tags": result["suggested_tags"],
            "rule_hints": result["rule_hints"],
            "state_diff": result["state_diff"],
        }

    def _run_combat_resolve(**kwargs: Any) -> dict:
        """combat_resolve：对抗/伤害/重伤/战技结算，check 权威区收集 + diff 抽走。

        状态：入口先做行动者白名单校验——只允许本轮行动者出手（恢复轮为挂起双方、
        反击命中后放行被反击方），越权直接拒绝并告警，杜绝代打与一轮多动；
        suspend 挂起只把攻击意图暂存 runner.pending_attack，恢复轮由注入补齐攻击参数。
        """
        # 状态：先合并挂起参数再做校验与结算——恢复轮的攻击参数由挂起补齐，
        # 校验必须针对真实出手者（合并后），否则恢复轮会拿不到 attacker 而漏判
        merged = _inject_pending_attack(kwargs)
        violation = _guard_actor(merged)
        if violation:
            logger.warning(
                "战斗越权行动被拒 world=%s 本轮可行动者=%s 请求=%s：%s",
                kwargs.get("world_id"), sorted(runner.actor_pool),
                merged.get("attacker_id"), violation,
            )
            return {"ok": False, "error": violation}
        result = _resolve(storage, merged, rng=rng)
        if result.get("check"):
            runner.collected_checks.append(result["check"])
        for extra in result.get("extra_checks") or []:
            runner.collected_checks.append(extra)
        # 状态：消费行动者白名单——主攻击用掉当前行动者，反击命中则放行被反击方换向再结算
        outcome = result.get("outcome") or {}
        runner.actor_pool.discard(merged.get("attacker_id"))
        if outcome.get("counter_hits") and merged.get("target_id"):
            runner.actor_pool.add(merged.get("target_id"))
        if result.get("suspend"):
            runner.pending_attack = result["suspend"]
        return {
            "ok": result["ok"],
            "summary": result["summary_for_agent"],
            "check": result["check"],
            "outcome": result.get("outcome"),
            "damage": result.get("damage"),
            "physical": result.get("physical"),
            "build": result.get("build"),
            "is_success": result.get("is_success"),
            "suspend": result.get("suspend"),
            "extra_checks": result.get("extra_checks") or [],
            "state_diff": result["state_diff"],
        }

    def _guard_actor(kwargs: Dict[str, Any]) -> Optional[str]:
        """行动者白名单校验：返回拒绝理由，合法则返回 None。

        状态：白名单为本轮可行动者集合——常规轮初始为当前行动者，恢复轮初始为
        挂起攻击方；每次结算消费一名，反击命中后放行被反击方（换向再结算）。
        """
        attacker = kwargs.get("attacker_id")
        if not attacker:
            return None  # 状态：交给 parse 报必填，不在白名单层抢断言
        # 状态：白名单缺失/为空一律拒绝——宁可误伤也不放行越权行动
        if not runner.actor_pool or attacker not in runner.actor_pool:
            allowed = sorted(x for x in (runner.actor_pool or set()) if x) or ["（无可行动者）"]
            return (
                f"本轮可行动者仅 {allowed}；{attacker} 不得在本轮出手"
                "（一次裁决只推进当前行动者的一次动作）"
            )
        return None

    def _inject_pending_attack(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """恢复轮：从挂起攻击补齐本次 strike 缺失的攻击参数（不涉攻击检定）。

        状态：挂起时不预投攻击，故无掷骰需沿用；这里只补武器/护甲等静态参数，
        防御声明由模型填入，攻击与防御检定由规则内核一并掷出。
        """
        merged = dict(kwargs)
        pending = runner.pending_attack or {}
        if pending:
            merged.setdefault("attacker_id", pending.get("attacker_id"))
            merged.setdefault("target_id", pending.get("target_id"))
            merged.setdefault("attack_skill", pending.get("attack_skill"))
            merged.setdefault("damage_expression", pending.get("damage_expression"))
            if not merged.get("damage_bonus"):
                merged["damage_bonus"] = pending.get("damage_bonus") or ""
            merged.setdefault("target_armor", pending.get("target_armor"))
            merged.setdefault("difficulty", pending.get("difficulty"))
            merged.setdefault("bonus_penalty_dice", pending.get("bonus_penalty_dice"))
        return merged

    def _run_tags(**kwargs: Any) -> dict:
        """manage_tags：增删动态状态标签（倒地/掩体等战术临时态）。"""
        result = _tags(storage, kwargs)
        return {
            "ok": result["ok"],
            "summary": result["summary_for_agent"],
            "tags_changed": result["tags_changed"],
            "state_diff": result["state_diff"],
        }

    async def _run_search_rule(**kwargs: Any) -> dict:
        return await _run_search_rule_tool(**kwargs)

    def _accept_present(**kwargs: Any) -> dict:
        """present_combat 兜底 handler：正常路径由 stop 收敛拦截，此函数仅防 stop 失效。"""
        return {"ok": True, "accepted": True, "present": kwargs}

    runner.register("search_rule", _run_search_rule)
    runner.register("check_and_update_stats", _run_stats)
    runner.register("combat_resolve", _run_combat_resolve)
    runner.register("manage_tags", _run_tags)
    runner.register(PRESENT_COMBAT_NAME, _accept_present)
    return runner


def _combat_resolve_schema() -> Dict[str, Any]:
    """combat_resolve 的 parameters（注入字段 world_id 不暴露）。"""
    p = get_prompt
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["strike", "maneuver", "suspend"],
                       "description": p("combat.params.combat_resolve.action")},
            "attacker_id": {"type": "string", "description": p("combat.params.combat_resolve.attacker_id")},
            "target_id": {"type": "string", "description": p("combat.params.combat_resolve.target_id")},
            "attack_skill": {"type": "string", "description": p("combat.params.combat_resolve.attack_skill")},
            "defense": {"type": "string", "enum": ["none", "dodge", "counterattack"],
                        "description": p("combat.params.combat_resolve.defense")},
            "defense_skill": {"type": "string",
                              "description": p("combat.params.combat_resolve.defense_skill")},
            "damage_expression": {"type": "string", "description": p("combat.params.combat_resolve.damage_expression")},
            "damage_bonus": {"type": "string", "description": p("combat.params.combat_resolve.damage_bonus")},
            "target_armor": {"type": "integer", "description": p("combat.params.combat_resolve.target_armor")},
            "difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"],
                           "description": p("combat.params.combat_resolve.difficulty")},
            "bonus_penalty_dice": {"type": "integer",
                                   "description": p("combat.params.combat_resolve.bonus_penalty_dice")},
            "attacker_build": {"type": "integer", "description": p("combat.params.combat_resolve.attacker_build")},
            "target_build": {"type": "integer", "description": p("combat.params.combat_resolve.target_build")},
            "maneuver_kind": {"type": "string", "description": p("combat.params.combat_resolve.maneuver_kind")},
        },
        "required": ["action", "attacker_id", "target_id"],
    }


def build_present_combat_schema() -> Dict[str, Any]:
    """构建 present_combat 收尾工具 schema；description 动态读配置（热重载生效）。"""
    p = get_prompt
    return {
        "type": "function",
        "function": {
            "name": PRESENT_COMBAT_NAME,
            "description": p("combat.present_combat"),
            "parameters": {
                "type": "object",
                "properties": {
                    "narrative_directive": {
                        "type": "string",
                        "description": p("combat.params.narrative_directive"),
                    },
                    "in_combat": {
                        "type": "boolean",
                        "description": p("combat.params.in_combat"),
                    },
                    "pending_reaction": {
                        "type": "object",
                        "description": p("combat.params.pending_reaction._desc"),
                        "properties": {
                            "attacker_id": {"type": "string", "description": p("combat.params.pending_reaction.attacker_id")},
                            "target_id": {"type": "string", "description": p("combat.params.pending_reaction.target_id")},
                            "attack_skill": {"type": "string", "description": p("combat.params.pending_reaction.attack_skill")},
                            "damage_expression": {"type": "string", "description": p("combat.params.pending_reaction.damage_expression")},
                            "damage_bonus": {"type": "string", "description": p("combat.params.pending_reaction.damage_bonus")},
                            "threat": {"type": "string", "description": p("combat.params.pending_reaction.threat")},
                        },
                        "required": ["attacker_id", "target_id", "attack_skill", "damage_expression"],
                    },
                },
                "required": ["narrative_directive"],
            },
        },
    }


def build_combat_schemas() -> List[Dict[str, Any]]:
    """Combat Agent 工具 schema：search/check/combat_resolve/manage_tags + present_combat 收尾。

    描述复用主 tools.* 与 combat.params.*，保证与主 Agent 同构且支持热重载。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "search_rule",
                "description": get_prompt("tools.search_rule"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": get_prompt("params.search_rule.query")},
                        "top_k": {"type": "integer", "description": get_prompt("params.search_rule.top_k")},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_and_update_stats",
                "description": get_prompt("tools.check_and_update_stats"),
                "parameters": _drop_combat_injected(_check_stats_params()),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "combat_resolve",
                "description": get_prompt("tools.combat_resolve"),
                "parameters": _combat_resolve_schema(),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "manage_tags",
                "description": get_prompt("tools.manage_tags"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entity_id": {"type": "string", "description": get_prompt("params.manage_tags.entity_id")},
                        "add_tags": {"type": "array", "items": {"type": "string"}, "description": get_prompt("params.manage_tags.add_tags")},
                        "remove_tags": {"type": "array", "items": {"type": "string"}, "description": get_prompt("params.manage_tags.remove_tags")},
                    },
                    "required": ["entity_id"],
                },
            },
        },
        build_present_combat_schema(),
    ]


def _check_stats_params() -> Dict[str, Any]:
    """复用主参数但剔除注入字段（world_id/turn_num 由协调器注入）。"""
    from src.tools.schemas import to_openai_function_schema as _stats_schema
    return _stats_schema()


def _drop_combat_injected(parameters: Dict[str, Any]) -> Dict[str, Any]:
    """从 parameters JSON Schema 剔除注入字段（world_id/turn_num）。"""
    props = dict(parameters.get("properties") or {})
    for k in ("world_id", "turn_num"):
        props.pop(k, None)
    required = [r for r in (parameters.get("required") or []) if r not in ("world_id", "turn_num")]
    out: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def _normalize_pending(raw: Any) -> Optional[dict]:
    """规范化挂起反应：必填四字段齐全才采纳，否则丢弃（防御坏契约污染软状态）。"""
    if not isinstance(raw, dict):
        return None
    req = ["attacker_id", "target_id", "attack_skill", "damage_expression"]
    if any(not raw.get(k) for k in req):
        return None
    return {
        "attacker_id": str(raw["attacker_id"]),
        "target_id": str(raw["target_id"]),
        "attack_skill": str(raw["attack_skill"]),
        "damage_expression": str(raw["damage_expression"]),
        "damage_bonus": str(raw.get("damage_bonus") or ""),
        "threat": str(raw.get("threat") or ""),
    }


def _extract_combat_present(result, fallback: str) -> Dict[str, Any]:
    """从收尾调用提取战斗契约：手记 / 是否维持战斗 / 挂起反应。"""
    args = (result.stop_call or {}).get("arguments") if result.stop_call else None
    if not isinstance(args, dict):
        return {"narrative": fallback, "in_combat": True, "pending_reaction": None}
    narrative = str(args.get("narrative_directive") or "").strip() or fallback
    in_combat = bool(args.get("in_combat", True))  # 状态：缺省维持战斗
    pending = _normalize_pending(args.get("pending_reaction"))
    return {"narrative": narrative, "in_combat": in_combat, "pending_reaction": pending}


# ============================================
# 战斗轮编排
# ============================================

_FORCE_DISENGAGE_HANDOFF = (
    "战斗在此处收束——参战一方已尽数失去行动能力，枪火与嘶吼归于沉寂。"
    "现场只余下狼藉与呼救，主动权完全交还玩家。"
)


def _disengage_turn(
    storage, world_id: str, turn: int, action: str,
) -> NarrativeDirective:
    """一方尽数失去行动能力时的强制脱战：不调用 LLM，落一轮脱战手记。

    状态：软状态注销走 end_combat；本轮以手记文本直接落库 + 演播，保证战场
    残局不卡死在战斗管线。返回契约供演播复用。
    """
    end_combat(storage, world_id)
    directive = NarrativeDirective(
        narrative_directive=_FORCE_DISENGAGE_HANDOFF,
        state_changes={},
        checks=[],
        turn_num=turn,
        converged=True,
    )
    apply_turn_change(
        storage, world_id, turn,
        diffs=[],
        context_data={"user": action, "directive": _FORCE_DISENGAGE_HANDOFF},
    )
    return directive


async def run_combat_turn(
    storage,
    world_id: str,
    action: str,
    *,
    llm: Optional[Any] = None,
    narrator: Optional[Narrator] = None,
    tier: Optional[str] = None,
    temperature: Optional[float] = None,
    turn_num: Optional[int] = None,
    recent_limit: Optional[int] = None,
    rng: Optional[object] = None,
    on_turn_committed=None,
) -> "CombatTurn":
    """执行一轮战斗相位：推进顺位 → Combat Agent 裁决 → 落库 → Narrator 演播。

    action 为本轮玩家输入（普通行动 / 对挂起反应的防御声明 / 脱战声明）。
    返回 CombatTurn（字段与 NarratedTurn 对齐：directive/narration）。
    物理真相经 state_diff 落库，软状态写回 combat_runtime；终局等信号不在此处理。
    """
    cc = read_combat(storage, world_id)
    if not cc.get("in_combat"):
        raise CombatError("当前世界不处于战斗状态")
    if narrator is None:
        narrator = Narrator(llm=llm)
    turn = turn_num if turn_num is not None else storage.next_turn_num(world_id)
    entities = {e["id"]: e for e in storage.get_entities(world_id)}
    # 状态：每轮重算围攻计数（软状态，只呈现不加骰）
    cc["outnumbered"] = count_outnumbered(cc, entities)
    # 状态：挂起反应非空即本轮为恢复结算轮——与上一轮 NPC 攻击同属一个行动回合
    pending = cc.get("pending_reaction") or None
    # 状态：取当前待行动者（不步进）；恢复轮游标仍停在发起攻击的 NPC 处
    current = current_actor(cc, entities)
    # 状态：对抗消失（一方尽数倒下）或全场无行动者 → 强制脱战，不调用 LLM
    if not opponents_remain(cc, entities) or current is None:
        directive = _disengage_turn(storage, world_id, turn, action)
        narration = await _narrate(storage, world_id, directive, narrator, action)
        return CombatTurn(directive=directive, narration=narration)
    # 状态：标识本轮是否玩家行动——决定是否在叙事末尾交还主动权
    current_entity = entities.get(current) or {}
    is_pc_turn = current_entity.get("type") == "PC"
    # 状态：恢复轮预置挂起攻击与行动者白名单——combat_resolve 将校验行动者并沿用挂起参数
    runner = build_combat_runner(storage, rng=rng, pending=pending, current=current)
    runner.reset_diffs()
    runner.reset_checks()
    messages = build_combat_messages(storage, world_id, cc, action, current, entities)
    settings = get_settings()
    _tier = tier or str(settings.get("context.combat.llm_tier", DEFAULT_TIER))
    _temperature = temperature if temperature is not None else settings.get("context.combat.temperature", None)
    result = await run_tool_loop(
        llm, _tier, messages, build_combat_schemas(), runner,
        world_id=world_id, turn_num=turn, temperature=_temperature,
        max_iterations=int(
            settings.get("context.combat.max_iterations", DEFAULT_MAX_ITERATIONS)
        ),
        stop_tool_name=PRESENT_COMBAT_NAME,
    )
    fallback = (result.final.text or "").strip() if result.final.is_ok else ""
    if not fallback and not result.converged:
        raise CombatError(f"战斗裁决失败: {result.final.error or '未知错误'}")
    contract = _extract_combat_present(result, fallback)
    # 状态：本轮是否产生新挂起——恢复轮清空挂起并完成结算；
    # 常规轮以本轮 suspend 产生的挂起为准（present_combat 手填仅作兼容兜底）
    if pending:
        pending_new = None
    else:
        pending_new = runner.pending_attack or contract["pending_reaction"] or None
    directive = NarrativeDirective(
        narrative_directive=contract["narrative"] or fallback,
        state_changes=_merged_diff(runner),
        checks=runner.collected_checks,
        turn_num=turn,
        converged=result.converged,
    )
    # 状态：落库语义对齐——NPC 自主轮无玩家输入可记，user 用占位符；
    # 仅 PC 回合或恢复轮（玩家声明防御）才记录玩家输入
    user_audit = action if (pending or is_pc_turn) else _AUTO_STEP_ACTION
    apply_turn_change(
        storage, world_id, turn,
        diffs=runner.collected_diffs,
        context_data={
            "user": user_audit,
            "directive": contract["narrative"] or fallback,
            "combat": {
                "in_combat": contract["in_combat"],
                "suspended": bool(pending_new),
                "resumed": bool(pending),
            },
        },
    )
    # 状态：行动横幅在本轮游标步进前渲染（此时 cursor 仍指向本轮行动者，
    # 横幅不进 Narrator 上下文，仅在演播后由程序拼接）
    banner = render_turn_banner(storage, world_id, cc, current, entities)
    # 状态：软状态更新——本轮产生挂起则停留原地（NPC 回合未收尾，等玩家声明防御），
    # 否则步进游标到下一顺位；账战则注销战场切片
    if not contract["in_combat"]:
        end_combat(storage, world_id)
    else:
        cc["pending_reaction"] = pending_new
        if not pending_new:
            advance_battlefield(cc, entities)
        save_combat(storage, world_id, cc)
    await get_trace_bus().publish(make_directive_event(
        directive.narrative_directive, world_id=world_id, turn_num=turn,
    ))
    # 状态：NPC 自主轮无玩家输入可播（不交还主动权）
    action_for_narration = action if (pending or is_pc_turn) else None
    narration = await _narrate(
        storage, world_id, directive, narrator, action_for_narration, banner=banner
    )
    if on_turn_committed is not None:
        try:
            await on_turn_committed(world_id, turn)
        except Exception as e:  # noqa: BLE001
            logger.error(f"战斗回合 on_turn_committed 钩子失败 world={world_id} turn={turn}: {e}", exc_info=True)
    return CombatTurn(directive=directive, narration=narration)


@dataclass
class CombatTurn:
    """一轮战斗相位的对外交付物（字段与 pipeline.NarratedTurn 对齐）。"""

    directive: NarrativeDirective
    narration: str


# 状态：NPC 自动回合的行动输入占位——战斗轮由调度器自动推进，此时无玩家输入
_AUTO_STEP_ACTION = "（战斗轮自动推进）"

# 玩家在 NPC 自主回合提交战斗声明的拒绝提示（程序附加，不进 Narrator 上下文）
_REJECT_NOTICE = (
    "（此刻仍是对手的行动段，你的声明未生效；"
    "轮到你行动或需要你声明防御时，再重新输入即可。）"
)


async def run_combat_batch(
    storage,
    world_id: str,
    action: str,
    *,
    llm: Optional[Any] = None,
    narrator: Optional[Narrator] = None,
    tier: Optional[str] = None,
    temperature: Optional[float] = None,
    turn_num: Optional[int] = None,
    recent_limit: Optional[int] = None,
    rng: Optional[object] = None,
    on_turn_committed=None,
    on_step_narrated=None,
    max_steps: int = 12,
) -> "CombatTurn":
    """战斗批量调度：先结算玩家行动（若轮到玩家），再自动推送后续 NPC 回合。

    玩家输入的消费点只有两处——有挂起时（防御声明）或轮到 PC 时（行动）；
    若当前轮到 NPC 且无挂起，玩家输入一律不进入结算（拒绝越权声明），
    本批以自动推进 NPC 为主，末尾附加拒绝提示后交还玩家。

    交还条件有三——轮回下一个 PC、触发挂起（NPC 攻击 PC 待玩家声明防御）、
    战斗终结（一方尽失行动能力）；任一命中即停止自动推送并把该步作为返回值。
    中间步（后面仍有 NPC 待动）经 on_step_narrated 即时外推，
    末尾步作为正常返回值交付，使 adapter 能逐动流式下发而非一次性抛出。

    每个行动者各自分配自增 turn_num 并独立落库，保证回档粒度精准可审计。
    max_steps 为防死循环的硬上限（异常战场规模下兑底，不静默吞步）。
    """
    # 状态：先判定玩家输入是否应被消费——仅挂起/轮到 PC 时有效；
    # 轮到 NPC 且无挂起时玩家的战斗声明一律拒绝，自动推进并附提示
    cc0 = read_combat(storage, world_id)
    entities0 = {e["id"]: e for e in storage.get_entities(world_id)}
    cur0 = current_actor(cc0, entities0) if cc0.get("in_combat") else None
    cur0_entity = entities0.get(cur0) or {}
    has_pending = bool(cc0.get("pending_reaction"))
    on_pc_turn = cur0 is not None and cur0_entity.get("type") == "PC"
    has_input = bool(action and str(action).strip())
    rejected = bool(has_input and not has_pending and cur0 is not None and not on_pc_turn)
    step_action = action if (has_pending or on_pc_turn) else _AUTO_STEP_ACTION

    def _finalize(res: Optional[CombatTurn]) -> Optional[CombatTurn]:
        """统一收尾：被拒绝的玩家声明在末尾附提示，保证玩家知道其输入未生效。"""
        if res is not None and rejected and res.narration:
            res.narration = res.narration.rstrip() + "\n\n" + _REJECT_NOTICE
        return res

    result: Optional[CombatTurn] = None
    for _ in range(max(1, int(max_steps))):
        result = await run_combat_turn(
            storage, world_id, step_action,
            llm=llm, narrator=narrator, tier=tier, temperature=temperature,
            turn_num=turn_num, recent_limit=recent_limit, rng=rng,
            on_turn_committed=on_turn_committed,
        )
        # 状态：每步结束重读软状态，判定是否还能继续自动推送
        cc = read_combat(storage, world_id)
        if not cc.get("in_combat"):
            return _finalize(result)  # 账战收束，交还玩家
        if cc.get("pending_reaction"):
            return _finalize(result)  # 挂起等玩家声明防御，交还玩家
        entities = {e["id"]: e for e in storage.get_entities(world_id)}
        nxt = current_actor(cc, entities)
        if nxt is None:
            return _finalize(result)
        if (entities.get(nxt) or {}).get("type") == "PC":
            return _finalize(result)  # 轮回玩家，交还主动权
        # 状态：下一顺位是 NPC → 本步为中间步，即时外推后继续自动推送
        if on_step_narrated is not None:
            try:
                await on_step_narrated(
                    world_id, result.directive.turn_num, result.narration
                )
            except Exception as e:  # noqa: BLE001  外推失败不影响战斗推送
                logger.error(
                    f"on_step_narrated 钩子失败 world={world_id}: {e}", exc_info=True
                )
        step_action = _AUTO_STEP_ACTION
        turn_num = None  # 状态：后续步各自分配自增轮号，逐动独立落库
    logger.warning("战斗批量调度触顶 world=%s 上限=%s", world_id, max_steps)
    return _finalize(result)


async def _narrate(
    storage, world_id, directive, narrator, action, *, banner: str = ""
) -> str:
    """Narrator 演播战斗手记为玩家文本，程序横幅前置拼接后回写 context_data。

    状态：横幅（行动顺位/开场公告）纯程序拼装，绝不进入 Narrator 上下文；
    action 为 None 表示本轮由 NPC 自主行动，无玩家输入可供播报。
    """
    recent_limit = int(get_settings().get("context.assembler.recent_turns", 10))
    recent = [
        t for t in storage.get_recent_turns(world_id, limit=recent_limit)
        if t["turn_num"] != directive.turn_num
    ]
    narration = await narrator.narrate(
        directive, recent=recent, action=action, world_id=world_id, combat=True,
    )
    text = f"{banner}\n\n{narration}" if banner else narration
    storage.update_turn_context_data(world_id, directive.turn_num, assistant=text)
    await get_trace_bus().publish(make_narration_event(text, world_id=world_id, turn_num=directive.turn_num))
    return text


def _merged_diff(runner) -> dict:
    """把 runner 收集的多份 state_diff 合并为一份契约镜像（仅展示用途，落库走 collected_diffs）。"""
    from src.storage.diff import empty_diff, merge_diff
    merged = empty_diff()
    for d in runner.collected_diffs:
        merge_diff(merged, d)
    return merged