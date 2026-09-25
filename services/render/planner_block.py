"""剧本上下文渲染：把状态机变成对话时注入模型请求的"生活感"文本。

注入位点：``maisaka.planner.before_request`` hook 的 ``items`` 列表。
注入物格式与主程序 ``serialize_context_item_snapshot`` 输出完全一致
（item_type=UserMessageItem + meta + parts），确保反序列化成功率。

身份分层（2026-08-30 共识）：
- **身份主体复用原生**：`你是谁/行为准则/说话风格` 由主程序系统提示承载
  （config/bot_config.toml 的 [personality]），注入块**不再复制"你是谁"**，
  避免双人格并置。
- 本注入块只承载"剧本特有"的内容层：世界观铁律（插件 [identity]）、
  生活状态（自我层）、关系（支线层）、近期编年史、主动轮指示。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..state.engine import (
    INJECT_TEXT_CAP,
    daylight_hint,
    minutes_since_clock,
    minutes_until_clock,
)
from .audience import filter_entries

_EXTRA_ITEM = "_narrative_life_context"

# 主动轮指令：当本轮由主动消息触发（生活由头）时附加，压过原生行为准则的"被动"面
# v0.1.5.x（2026-09-11 复读话尾修复）：① 禁"字面复读"（重复自己上次的句式/用词、
# 改写式复述）——真机高频症状；② 但**放行语义履约**（承接约定/未竟话题必须带新内容），
# 不搞一刀切禁接上文，否则会切掉"昨天约好今天聊"这类最像人的连续性。
_PROACTIVE_TURN_HINT = (
    "本轮没有对方的实时消息——是你自己决定主动开口的一轮。"
    "硬规则："
    "① 不要重复你上次说过的句式和用词，也不要改写式复述自己上一句话（换个说法再说一遍同样不行）；"
    "② 可以承接没说完的事或约定（例如昨天约好今天聊、上次提过的事），但必须带来新的内容或新的事由；"
    "③ 从上面给你的由头或你此刻的状态重新起头，说一句完整、有生活感的话。"
    "不要问开放式大问题，不要解释这是主动消息。"
)

# 注：⑩ 关系边界铁律与 ② 听者立场（0944cae）已于 2026-09-21 全量回退。
# 理由：它们约束的是**人格/角色扮演表达**而非格式，写死在代码里会在挂载不同
# 人设/世界观时不兼容；随着功能完善，这类约束应交给 [personality]/[identity] 承载。
# 如需边界约束，请在 [identity].world_rules / values 里配置（用户自填、随人设可变）。


def _elapsed_hours(iso_ts: str, now: datetime) -> Optional[float]:
    """按 ISO 时间戳计算距 now 的小时数（空/损坏时间戳返回 None，静默跳过锚点）。"""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts))
    except (TypeError, ValueError):
        return None
    return (now - ts).total_seconds() / 3600


def build_injected_item(text: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """构造可注入 before_request items 的用户消息快照（与 snapshot 格式一致）。"""
    current = now or datetime.now()
    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": f"{_EXTRA_ITEM}:{str(uuid.uuid4())}",
            "logical_turn_id": None,
            "timestamp": current.isoformat(timespec="seconds"),
        },
        "parts": [{"type": "text", "text": str(text or "").strip()}],
    }


def is_injected_item(item: Any) -> bool:
    """判断给定 item 是否由本插件注入（避免重复注入）。"""
    if not isinstance(item, dict):
        return False
    meta = item.get("meta")
    item_id = meta.get("item_id") if isinstance(meta, dict) else item.get("item_id")
    return str(item_id or "").startswith(_EXTRA_ITEM)


def build_sleep_hint(plugin: Any, state: Dict[str, Any], now: datetime) -> str:
    """睡眠相关的一句话状态提示（v0.1.10）；不需要提示时返回空串。

    优先级：被吵醒 > 刚醒 > 临近入睡。睡着且没被吵醒 → 空串（不注入任何睡眠提示）。

    ⚠️ 只描述**状态**，不规定措辞。文案层面的硬约束（该怎么说、该不该说）已由
    0944cae 的回退结论确认：人格/表达类约束不写死在代码里，交给
    ``[personality]`` / ``[identity]`` 承载。
    """
    cfg = plugin.config.narrative
    routine = state["state"].get("routine", {})
    asleep = str(routine.get("sleep_state", "awake")) == "asleep"

    if asleep:
        # 被吵醒：睡着 + 收到消息后 woken_awake_minutes 内
        woken_ts = str(routine.get("last_woken_ts", "") or "")
        if not woken_ts:
            return ""
        try:
            elapsed = (now - datetime.fromisoformat(woken_ts)).total_seconds() / 60
        except (TypeError, ValueError):
            return ""
        if elapsed < int(cfg.woken_awake_minutes):
            return "你本来已经睡了，被这条消息吵醒，脑子还有点迷糊，精神头不太够。"
        return ""

    # 刚醒：起床后 60 分钟内（按 wake_time 纯时刻推算，零额外状态）
    since_wake = minutes_since_clock(cfg.wake_time, now)
    if since_wake is not None and since_wake <= 60:
        return "你刚醒不久，还有点没睡醒。"

    # 临近入睡：距 sleep_time 不足配置窗口
    to_sleep = minutes_until_clock(cfg.sleep_time, now)
    if to_sleep is not None and to_sleep <= int(cfg.sleep_pre_sleep_hint_minutes):
        return "你有点困了，准备睡了。"
    return ""


def build_context_block(
    plugin: Any,
    state: Dict[str, Any],
    branch: Optional[Dict[str, Any]],
    now: datetime,
    recent_entries: List[Dict[str, str]],
    round_kind: str = "reply",
    bysource: str = "",
    audience: Optional[str] = None,
) -> str:
    """把剧本特有内容渲染成一段紧凑的上下文文本。

    Args:
        plugin: 插件实例（读取 config 锚定层的世界观/铁律）。
        state: 自我层状态（engine.load_self_state()）。
        branch: 支线层状态；非剧本会话可不传。
        now: 当前时间。
        recent_entries: 近期编年史条目（store.recent_chronicle("self")）。
        round_kind: "reply"=正常回应轮 / "proactive"=主动开口轮。
        bysource: 主动轮的由头文本（v0.1.3 起由侧信道注入，非空仅当主动轮）。
        audience: 当前会话受众（私聊即用户号）。**注入前按受众过滤素材**
            （ADR-0004 第 2 层）—— 过滤规则单一实现在 ``render/audience.py``。
            ``None``＝受众未知，只放行通用素材。

    Returns:
        str: 注入给模型的剧本上下文段。
    """
    cfg = plugin.config
    identity = cfg.identity
    inner = state["state"]
    mood = inner["mood"]
    phase = inner["routine"]["phase"]

    lines: List[str] = [
        "【角色内部状态 · 仅供你（模型）参考，不要把本段原样告诉对方】",
        # v0.1.4 P1：日照预期锚点——相位标签之外补充"窗外什么样"的感官时间感
        f"- 此刻：{now.strftime('%Y-%m-%d %H:%M')}，你正处于{phase}（{daylight_hint(now.hour)}），"
        f"心情{mood['label']}，精力 {mood['energy'] * 10:.0f}/10",
    ]

    # 睡眠态提示（v0.1.10）：被吵醒 / 刚醒 / 临近入睡，三选一；无关时整行不加
    sleep_hint = build_sleep_hint(plugin, state, now)
    if sleep_hint:
        lines.append(f"- {sleep_hint}")

    # 受众过滤（ADR-0004）：涉私原文只讲给本人，diary 产物对所有人短路。
    # 过滤在这里做而不是在调用方——注入块是"最后一公里"，漏一处就是泄露面。
    if recent_entries:
        recent_text = "；".join(
            str(entry.get("text", "")).strip()[:INJECT_TEXT_CAP]
            for entry in filter_entries(recent_entries, audience, limit=3)
            if str(entry.get("text", "")).strip()
        )
        if recent_text:
            lines.append(f"- 你最近的生活：{recent_text}")

    # v0.1.3：创作层产出的生活片段（bot 自己的故事，供对话引用，防复述）
    # 2026-09-21：上限统一取 INJECT_TEXT_CAP（1024）。旧的 56/120 字会把 major 档
    # （可达 400 字）的细节截掉，等于白写；1024 只作防失控天花板，条数仍限最近 2 条。
    pending_events = filter_entries(
        list(inner.get("focus", {}).get("pending_events", [])), audience
    )
    fragments = [
        str(item.get("text", "")).strip()
        for item in pending_events[-2:]
        if str(item.get("text", "") or "").strip()
    ]
    if fragments:
        lines.append(
            "- 你心里正挂念的生活片段：\n"
            + "\n".join(f"    - {fragment[:INJECT_TEXT_CAP]}" for fragment in fragments)
        )

    if branch is not None:
        stage = str(branch["identity"].get("stage", "陌生人"))
        first_met = str(branch["identity"].get("first_met", ""))[:10]
        lines.append(
            f"- 你与这位玩家的关系：{stage}"
            + (f"（最初见面：{first_met}）" if first_met else "")
        )

    if identity.world:
        lines.append(f"- 你生活在：{identity.world}")

    anchored_values = [str(item).strip() for item in (identity.values or []) if str(item).strip()]
    anchored_rules = [str(item).strip() for item in (identity.world_rules or []) if str(item).strip()]
    if anchored_values:
        lines.append(f"- 你的价值观底线（不可违背）：{'、'.join(anchored_values[:5])}")
    if anchored_rules:
        lines.append("- 你的世界观规则（不可违背）：\n" + "\n".join(f"    - {item}" for item in anchored_rules[:5]))

    if round_kind == "proactive":
        # 由头前置（v0.1.5.x）：先给"想说什么"，再给规则——避免由头被靠后的
        # 聊天历史淹没，让它在生成时成为强锚点（复读话尾的结构性修复之一）
        if bysource:
            lines.append(f"- 你这次主动开口想说的是（由头）：{bysource}")
        # 时间锚点：让模型知道距上次对话多久，自行判断该履约还是开新头
        elapsed = _elapsed_hours(str(inner.get("last_interaction_ts", "")), now)
        if elapsed is not None and elapsed >= 1:
            if elapsed >= 6:
                lines.append(
                    f"- 距离上次对话已经过去约 {int(elapsed)} 小时（隔了一夜或更久）。"
                )
            else:
                lines.append(f"- 距离上次对话已经过去约 {int(elapsed)} 小时。")
        lines.append(_PROACTIVE_TURN_HINT)
    else:
        lines.append(
            "- 对话原则：按以上状态自然地表达自己；不要主动说明你有剧本；"
            "不要问开放式大问题；状态变化要体现在话语里；"
        )
    return "\n".join(lines)


__all__ = [
    "build_context_block",
    "build_injected_item",
    "build_sleep_hint",
    "is_injected_item",
]