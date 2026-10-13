"""replyer 漂移调制注入块 —— 批 3 落地（ADR-0003 §5、§6；执行路线批 3）。

注入位点：``maisaka.replyer.before_model_request`` hook 的 ``items`` 列表。

**与 planner 块的分工（E9，防同一事实双重注入）**：

| 块 | marker | 内容 |
|---|---|---|
| planner 块 | ``_narrative_life_context`` | 生活内容 / 编年史 / 由头（**发生了什么**） |
| replyer 块（本模块） | ``_narrative_style_`` | 此刻状态调制 / 文学授权（**这轮怎么表达**） |

「关系语境」行已摘除（2026-10-13 用户裁定）：实机期关系在人设提示词层写死，
动态注入的「关系阶段 = 陌生人」与写死关系打架且放大攻击性误读；关系亲密推进
设计（含阶段呈现方式）留待部署线下单独 branch 重做。

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


def build_replyer_block(
    *,
    drift_text: str,
    learned_style: Sequence[str] = (),
) -> str:
    """把漂移层要素组装成注入块文本（零 LLM，纯确定性）。

    结构：调制段 → `[learned]` 槽（空则不渲染）。
    
    **W2 裁定（2026-10-13）**：停用授权段与 learned_style 注入——漂移层只留事实状态
    （心情/精力/作息相位），不再渲染「长期表达倾向」槽。本函数保留参数接口供未来
    扩展，但实际调用方已传入空序列（见 `inject.py`）。
    """
    parts: List[str] = []
    modulation = str(drift_text or "").strip()
    if modulation:
        parts.append(modulation)
    # W2：learned_slot 停用注入，整段不渲染（原逻辑：if learned_style: ...）
    return "\n".join(parts)
