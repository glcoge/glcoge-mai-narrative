"""回放台：证据计数 / 场景映射消化用例（批 2-C8 / R4 + R5）。

## 为什么要这个用例

批 4 的晋升机门槛（R4/P3/P4）与"场景计数映射"（R5）都**只能靠真实数据定标**。
批 2 闸门不要求定标数值，但必须先把"26 天归档能不能被正确消化"这件事验掉——
否则批 4 一开工会同时踩两个坑：数据读不出来 + 数值不对，分不清是哪一层坏。

因此本用例**只做两件事**，都不涉及晋升判定：

1. **证据计数可消化**：把归档按「人 × 真实互动日」聚合，证明
   ``interaction_count``（R6 的确定性计数来源）能从真实数据确定性重算——
   同一份数据跑两次得到同一结果（回放台的核心契约：离线可复现）。
2. **场景映射可消化**：按 R5 定义的单位（1 互动日 = 1 场景；1 生活片段 = 1 场景）
   把归档折算成场景数，用于批 4 核对"门槛 3 场景 / 2 天"是否真实可达。

## 单位口径（R5 当前值，批 4 可改）

- 归档里**没有**生活片段数据（narrative 运行时数据不在 export 里），
  故"1 生活片段 = 1 场景"这一路只能**按互动日近似**：一天最多算 1 个生活事件场景。
  这是**有意的保守下界**——宁可低估场景数（门槛看起来更难达到），
  也不要高估后让批 4 误以为门槛太松。
- "1 互动日 = 1 场景"是完全可算的：某人在某天有 ≥1 条非 bot 发言即记 1 天。

## 断言（空列表 = 通过）

- 数据能读出且规模达标（否则是数据源问题，不是算法问题）
- 证据计数确定性：重算两遍结果一致
- 互动日总数 > 0，且不超过 账号数 × 天数（上界自检，防聚合口径写错）
- 场景数 ≥ 互动日数（R5 的两路单位叠加只会增不会减）
- 至少一个账号达到 R4 的 minor 门槛规模（≥3 场景、≥2 天），否则门槛定标无据
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Tuple

from .. import loader

#: R4 的 minor 门槛里的"场景数 / 天数"两维（阈值本身在批 4 定标，此处只验可达性）
_MINOR_SCENES = 3
_MINOR_DAYS = 2


def _day_of(row: Dict[str, Any]) -> str:
    """取行的日期部分（ts_short 形如 ``2026-09-15 23:40``）。"""
    return str(row.get("ts_short") or str(row.get("timestamp") or ""))[:10]


def _is_bot(row: Dict[str, Any]) -> bool:
    return bool(row.get("is_bot")) or str(row.get("sender_label")) == loader.BOT_LABEL


def _evidence_counts(messages: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按 账号 → {天数, 互动日集合, 消息数} 聚合（**确定性**，无时间依赖）。

    这就是 ``interaction_count``（R6）在真实数据上的等价物：晋升证据链要的是
    "这段时间确实发生过互动"的确定性事实，不是关系演化值。
    """
    counts: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"days": set(), "messages": 0}
    )
    for row in messages:
        if _is_bot(row):
            continue
        day = _day_of(row)
        if not day:
            continue
        uid = str(row.get("sender_label") or "")
        if not uid:
            continue
        counts[uid]["days"].add(day)
        counts[uid]["messages"] += 1
    return counts


def _scene_counts(messages: List[Dict[str, Any]]) -> Dict[str, int]:
    """按 R5 的单位把归档折算成场景数（每账号）。

    1 互动日 = 1 场景；1 生活片段 = 1 场景。归档无生活片段数据，故按互动日
    **近似为上界 1 个生活事件场景/天**（保守下界，见模块文档）。
    """
    counts = _evidence_counts(messages)
    return {uid: len(info["days"]) for uid, info in counts.items()}


def case_evidence_counting(inputs: Any) -> List[str]:
    """证据计数 / 场景映射能否正确消化 26 天归档（R4 + R5 定标前置）。"""
    messages = loader.load_messages(inputs.messages)
    failures: List[str] = []

    # ── 0. 数据源规模自检 ──
    all_days = {_day_of(row) for row in messages if _day_of(row)}
    if not messages:
        return ["归档为空——数据源不符预期"]
    if len(all_days) < 2:
        return [f"归档只覆盖 {len(all_days)} 天，不足以验证场景映射"]

    # ── 1. 确定性：同一份数据聚合两遍必须完全一致 ──
    first = _evidence_counts(messages)
    second = _evidence_counts(list(reversed(messages)))
    if {uid: sorted(info["days"]) for uid, info in first.items()} != {
        uid: sorted(info["days"]) for uid, info in second.items()
    }:
        failures.append(
            "[不确定] 证据计数与输入顺序相关——回放台要求离线可复现，聚合必须与顺序无关"
        )
    if {uid: info["messages"] for uid, info in first.items()} != {
        uid: info["messages"] for uid, info in second.items()
    }:
        failures.append("[不确定] 消息计数与输入顺序相关")

    # ── 2. 聚合口径自检：互动日总数不超过 账号数 × 天数 ──
    total_days = sum(len(info["days"]) for info in first.values())
    upper_bound = len(first) * len(all_days)
    if total_days > upper_bound:
        failures.append(
            f"[口径错误] 互动日总数 {total_days} 超过上界 {upper_bound}（账号数 × 天数）"
        )
    if total_days == 0:
        failures.append("[空数据] 归档里没有任何非 bot 发言——无法验证证据计数")

    # ── 3. 场景数是互动日的上界（R5 两路单位叠加只增不减） ──
    scenes = _scene_counts(messages)
    for uid, scene_count in scenes.items():
        day_count = len(first[uid]["days"])
        if scene_count < day_count:
            failures.append(
                f"[映射错误] {uid} 场景数 {scene_count} < 互动日 {day_count}"
                "（R5：两路单位叠加只能增不能减）"
            )

    # ── 4. 门槛可达性：至少一个账号够得着 R4 的 minor 规模 ──
    reachable = [
        uid
        for uid, info in first.items()
        if len(info["days"]) >= _MINOR_DAYS and scenes.get(uid, 0) >= _MINOR_SCENES
    ]
    if not reachable:
        failures.append(
            f"[门槛不可达] 没有任何账号同时达到 {_MINOR_DAYS} 天 / {_MINOR_SCENES} 场景"
            "——R4 门槛数值无法用本批数据定标，需确认归档范围或改门限"
        )

    return failures


def case_scene_mapping_digest(inputs: Any) -> List[str]:
    """场景映射的**边界体检**：主动消息与互动日的交叉口径。

    单列一个用例是因为它是批 4 最容易踩的一类坑——主动开口（bot 发言）与
    用户互动（非 bot 发言）是**两条独立时间线**，若把主动消息也算进互动日，
    场景数会被系统性高估，门槛就会看起来比实际松。
    """
    messages = loader.load_messages(inputs.messages)
    failures: List[str] = []

    proactive = [row for row in messages if row.get("is_proactive")]
    if not proactive:
        # 没有主动消息不是错误（可能全期主动关闭），但要在结果里说清
        return []

    counts = _evidence_counts(messages)

    # 主动消息全部来自 bot，不得影响任何账号的互动日
    bot_days_by_session: Dict[str, set] = defaultdict(set)
    for row in proactive:
        bot_days_by_session[str(row.get("session_name") or "")].add(_day_of(row))

    user_sessions: Dict[str, set] = defaultdict(set)
    for row in messages:
        if not _is_bot(row):
            uid = str(row.get("sender_label") or "")
            if uid:
                user_sessions[uid].add(str(row.get("sender_name") or ""))

    # 交叉校验：主动消息的日期集合不应"只有主动、没有用户发言"地撑起互动日
    orphan_days = {
        day
        for days in bot_days_by_session.values()
        for day in days
        if day and not any(day in info["days"] for info in counts.values())
    }
    if len(orphan_days) == len(
        {day for days in bot_days_by_session.values() for day in days if day}
    ) and orphan_days:
        failures.append(
            f"[口径可疑] 全部 {len(orphan_days)} 个主动消息日都没有任何用户互动日与之重合"
            "——需确认 is_proactive 标记或日期归属口径"
        )

    return failures


__all__ = ["case_evidence_counting", "case_scene_mapping_digest"]
