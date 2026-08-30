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

_EXTRA_ITEM = "_narrative_life_context"

# 主动轮指令：当本轮由主动消息触发（生活由头）时附加，压过原生行为准则的"被动"面
_PROACTIVE_TURN_HINT = (
    "本轮没有对方的实时消息——是你自己决定主动开口的一轮。"
    "请按上面的生活状态自然地开启话题（可以从你正在想的事情/今天发生的事聊起），"
    "说一句完整、有生活感的话，不要问开放式大问题，不要解释这是主动消息。"
)


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


def build_context_block(
    plugin: Any,
    state: Dict[str, Any],
    branch: Optional[Dict[str, Any]],
    now: datetime,
    recent_entries: List[Dict[str, str]],
    round_kind: str = "reply",
) -> str:
    """把剧本特有内容渲染成一段紧凑的上下文文本。

    Args:
        plugin: 插件实例（读取 config 锚定层的世界观/铁律）。
        state: 自我层状态（engine.load_self_state()）。
        branch: 支线层状态；非剧本会话可不传。
        now: 当前时间。
        recent_entries: 近期编年史条目（store.recent_chronicle("self")）。
        round_kind: "reply"=正常回应轮 / "proactive"=主动开口轮。

    Returns:
        str: 注入给模型的剧本上下文段。
    """
    cfg = plugin.config
    identity = cfg.identity
    inner = state["state"]
    mood = inner["mood"]
    phase = inner["routine"]["phase"]
    hot_thread = str(inner["focus"].get("hot_thread", "")).strip()

    lines: List[str] = [
        "【角色内部状态 · 仅供你（模型）参考，不要把本段原样告诉对方】",
        f"- 此刻：{now.strftime('%Y-%m-%d %H:%M')}，你正处于{phase}，"
        f"心情{mood['label']}，精力 {mood['energy'] * 10:.0f}/10",
    ]

    if hot_thread:
        lines.append(f"- 你现在心里正想着：{hot_thread}")

    if recent_entries:
        recent_text = "；".join(
            str(entry.get("text", "")).strip()[:48]
            for entry in recent_entries[:3]
            if str(entry.get("text", "")).strip()
        )
        if recent_text:
            lines.append(f"- 你最近的生活：{recent_text}")

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
    anchored_traits = [str(item).strip() for item in (identity.immutable_traits or []) if str(item).strip()]
    if anchored_values:
        lines.append(f"- 你的价值观底线（不可违背）：{'、'.join(anchored_values[:5])}")
    if anchored_rules:
        lines.append("- 你的世界观规则（不可违背）：\n" + "\n".join(f"    - {item}" for item in anchored_rules[:5]))
    if anchored_traits:
        lines.append(f"- 你的人格底色（如与原生设定冲突，以本条为准）：{'、'.join(anchored_traits[:5])}")

    if round_kind == "proactive":
        lines.append(_PROACTIVE_TURN_HINT)
    else:
        lines.append(
            "- 对话原则：按以上状态自然地表达自己；不要主动说明你有剧本；"
            "不要问开放式大问题；情绪和状态变化要体现在话语里。"
        )
    return "\n".join(lines)


__all__ = [
    "build_context_block",
    "build_injected_item",
    "is_injected_item",
]