"""replyer 漂移调制注入块 —— 批 3 落地（ADR-0003 §5、§6；执行路线批 3）。

注入位点：``maisaka.replyer.before_model_request`` hook 的 ``items`` 列表。

**与 planner 块的分工（E9，防同一事实双重注入）**：

| 块 | marker | 内容 |
|---|---|---|
| planner 块 | ``_narrative_life_context`` | 生活内容 / 编年史 / 由头（**发生了什么**） |
| replyer 块（本模块） | ``_narrative_style_`` | 此刻状态调制 / 关系语境 / 文学授权（**这轮怎么表达**） |

两块 marker **不互为子串** → 各自的幂等检查只看自己，互不误判、互不覆盖。

item 格式与 ``planner_block.build_injected_item`` 一致（H10 契约：item_type + meta + parts，
与宿主 ``deserialize_context_item_snapshot`` 匹配；宿主侧经
``validate_context_items(REQUEST_CONTEXT)``，item_id 必须唯一）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

#: item_id 前缀（marker）。与 ``_narrative_life_context`` 不互为子串——已验
STYLE_ITEM = "_narrative_style_"

# ─── 文学授权段（批 3-C6 / ADR-0003 §6）────────────────────────────────────
#
# ❗ 本段是「**只增不删**」的回归护栏对象：任何提示词精简/压缩都不得删掉它。
#    依据：HDSI 1.7B/C 教训——每次压缩都会**不可见地**砍掉文学授权，正文随之干瘪；
#    故文案写死在代码里（不做成配置项，防「顺手精简」），并由
#    ``pytests/test_replyer_block.py::test_authorization_always_present`` 守住。
#
# ❗ 性质边界（用户 2026-09-26 纠偏）：只停在**叙事授权**（铺陈 / 节奏 / 深度），
#    **不得越界到人格约束**——人格 / 角色扮演类约束不写死代码（0944cae 回退铁律），
#    交 ``[personality]`` / ``[identity]`` 承载。
AUTHORIZATION_TEXT = (
    "以上只关乎表达节奏与详略的着色：你可以顺着当下的感觉铺陈、留白，"
    "也可以往深处多讲一层——不必每句都求完整。"
)

#: `[learned]` 槽（R1）的渲染标题；槽空则整段不渲染  # RESERVED(R1)
_LEARNED_TITLE = "长期表达倾向"


def build_style_item(text: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """构造可注入 replyer 请求 items 的用户消息快照（格式同 planner 块）。"""
    current = now or datetime.now()
    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": f"{STYLE_ITEM}:{uuid.uuid4()}",
            "logical_turn_id": None,
            "timestamp": current.isoformat(timespec="seconds"),
        },
        "parts": [{"type": "text", "text": str(text or "").strip()}],
    }


def is_style_item(item: Any) -> bool:
    """判断给定 item 是否由本模块注入（幂等检查用；不误认 planner 块）。"""
    if not isinstance(item, dict):
        return False
    meta = item.get("meta")
    item_id = meta.get("item_id") if isinstance(meta, dict) else item.get("item_id")
    return str(item_id or "").startswith(STYLE_ITEM)


def relationship_line(stage: str, *, audience: str, owner: str) -> str:
    """关系语境一句话（C4）：只描述**状态**，不规定表达。

    可见性 fail-closed（ADR-0004）：只对支线归属人本人可见；群聊 / 他人视角返回空串
    ——「你们是什么关系」这条信息对第三方不该出现（v0.2 群聊就绪纪律）。

    Args:
        stage: 关系阶段标签（``current_relationship_stage`` 产物）。
        audience: 当前注入面对的受众标识（私聊为本人 uid，群聊为 ``g:<id>``）。
        owner: 该关系事实的归属人 uid。
    """
    text = str(stage or "").strip()
    if not text:
        return ""
    if not owner or not audience or str(audience) != str(owner):
        return ""
    return f"关系阶段：{text}。"


def build_replyer_block(
    *,
    drift_text: str,
    stage: str,
    audience: str,
    owner: str,
    learned_style: Sequence[str] = (),
) -> str:
    """把漂移层要素组装成注入块文本（零 LLM，纯确定性）。

    结构：调制段 → 关系语境 → 授权段 → `[learned]` 槽（空则不渲染）。
    **授权段恒定在列**（只增不删），见 ``AUTHORIZATION_TEXT`` 注释。
    """
    parts: List[str] = []
    modulation = str(drift_text or "").strip()
    if modulation:
        parts.append(modulation)
    relation = relationship_line(stage, audience=audience, owner=owner)
    if relation:
        parts.append(relation)
    parts.append(AUTHORIZATION_TEXT)
    if learned_style:
        items = [str(item).strip() for item in learned_style if str(item).strip()]
        if items:
            parts.append(f"（{_LEARNED_TITLE}）" + "；".join(items))
    return "\n".join(parts)
