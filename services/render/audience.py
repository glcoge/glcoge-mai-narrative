"""素材可见性规则表（ADR-0004 第 2 层「读取过滤」，**全局单一实现**）。

批 0 只留占位；批 1（本笔）落地真实规则。

为什么单独成模块：注入侧（planner_block）、取材侧（engine / proactive）、
对外面（status / diary API）都要判定同一件事——"这条素材能不能讲给当前受众"。
规则一旦散落多处，漏一处就是一个泄露面（本批的事故就是这么发生的）。
故**所有**消费路径必须走本模块的 ``is_visible`` / ``filter_entries``，
store 侧刻意不做 SQL 过滤（见 ``services/store.py`` 的注释）。

规则表（逐条对应 ADR-0004 §2）：

| 素材 | 判定 | 可注入范围 |
|---|---|---|
| chronicle ``kind=diary`` | 恒 False（**含来源者本人**） | 完全隔离（用户裁定：diary 是后置附属） |
| 带 ``source_uid == SOURCE_DIARY`` | 恒 False | 同上（写入侧自动打标的兜底） |
| 带 ``source_uid=A``（涉私原文） | ``A == 当前受众`` | 仅 A 的会话 |
| 带 ``audience`` 列 | ``audience == 当前受众`` | 仅该受众 |
| 其余（无标签） | 恒 True | 通用（全用户） |

⚠ ``audience=None``（受众未知）≠ 放行全部：只放行**通用**条目。

🔑 ``source_uid`` 是**隔离标记**，不是溯源字段（2026-09-25 批 1 实现时定）：
ADR-0004 表格里「chronicle kind=life ｜ source_uid 溯源 ｜ 通用」与铁律
「涉私内容不进生活线」直接冲突——只要来源标签还挂在条目上，就无法区分
"消化产出（应通用）"与"涉私原文（应隔离）"。故拆成两个字段：

- ``source_uid``：**隔离标记**。非空即按受众过滤，与 kind 无关（fail-closed）。
- ``sources``：**溯源清单**（R19/Q2）。只供批 4 的学习输入掩码追溯，**不参与可见性判定**。

创作层产出（``kind`` ∈ ``GENERAL_KINDS``）**默认不带** ``source_uid``，因此天然通用；
若确为涉私原文则必须打标，此时按上表第 3 行隔离。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from ..store import SOURCE_DIARY

__all__ = [
    "SOURCE_DIARY",
    "DIARY_KIND",
    "GENERAL_KINDS",
    "SLOW_FIELD_AUDIENCE",
    "is_diary_entry",
    "is_visible",
    "filter_entries",
    "drop_diary",
    "is_slow_field_visible",
    "visible_chronicle",
    "visible_events",
]

#: chronicle 中表示 diary 产物的 kind（与 store 的隔离标记同源）
DIARY_KIND = "diary"

#: 创作层**消化产出**的 kind（已脱离原文形态）。这些产出**默认不带** ``source_uid``，
#: 故天然通用；批次 4 的溯源走独立字段 ``sources``（不参与可见性判定）。
#: 新增产出类型应登记到这里（当前仅作文档与写入侧约定，判定不读它——
#: 判定一律 fail-closed：带 source_uid 就按受众过滤，与 kind 无关）。
GENERAL_KINDS = frozenset({"life", "daily"})

#: 过滤后可能不足 limit（被过滤掉的条目仍占位），故按倍数多取再截断。
_OVERFETCH = 3


# ─── 核心判定 ──────────────────────────────────────────────────


def is_diary_entry(entry: Dict[str, Any]) -> bool:
    """是否为 diary 产物（ADR-0004「完全隔离」：对**所有**消费路径短路）。"""
    if str(entry.get("kind") or "") == DIARY_KIND:
        return True
    return str(entry.get("source_uid") or "") == SOURCE_DIARY


def is_visible(entry: Dict[str, Any], audience: Optional[str]) -> bool:
    """判定一条素材对 ``audience`` 是否可见。

    Args:
        entry: 素材条目（chronicle 行 / pending_events 项 / event 行）。
        audience: 当前会话受众（私聊即用户号）；``None``/空＝受众未知，只看通用。

    Returns:
        bool: True=可见。
    """
    if is_diary_entry(entry):
        return False
    target = str(audience or "").strip()
    # source_uid 是隔离标记：非空即按受众过滤，与 kind 无关（fail-closed）
    source_uid = str(entry.get("source_uid") or "").strip()
    if source_uid:
        return bool(target) and source_uid == target
    tagged_audience = str(entry.get("audience") or "").strip()
    if tagged_audience:
        return bool(target) and tagged_audience == target
    return True


def filter_entries(
    entries: Iterable[Dict[str, Any]],
    audience: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """按受众过滤素材序列（保持原顺序），可选截断到 ``limit`` 条。"""
    visible = [entry for entry in entries if is_visible(entry, audience)]
    if limit is None:
        return visible
    return visible[: max(0, limit)]


def drop_diary(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """只做 ADR-0004 的**最低隔离**（去 diary 产物），不过滤涉私。

    唯一用途：``narrative_diary_context`` 对外 API —— 用户 2026-09-25 裁定
    （Q1-b / R18）该 API **维持现状**，继续给 diary 侧编年史原文；但"diary 产物
    完全隔离"是立项铁律，不随该裁定豁免，故此处只短路 diary。
    """
    return [entry for entry in entries if not is_diary_entry(entry)]


# ─── 慢变区字段（D4 预留，批 4 接线） ───────────────────────────


#: 慢变字段 → 可见性口径（ADR-0002）。批 4 晋升机落地后由 ``is_slow_field_visible`` 消费。
#: 现在就把结构定死，防批 4 返工过滤层。
SLOW_FIELD_AUDIENCE: Dict[str, str] = {
    # 晋升时已用「证据纪律五条」排除私聊原文 → 通用
    "world_view": "general",
    "life_goals": "general",
    # 关系阶段天然 per-user
    "relationship": "per_user",
}


def is_slow_field_visible(
    field: str, audience: Optional[str], owner_uid: str = ""
) -> bool:
    """慢变字段的可见性（**批 4 才接线**，本批无调用点）。

    ⚠ **fail-closed**：未登记字段一律不可见。新慢变字段必须先在这里登记再读取，
    否则晋升后的内容会静默消失（比泄露好，且会在回放台暴露）。
    """
    scope = SLOW_FIELD_AUDIENCE.get(str(field or ""))
    if scope is None:
        return False
    if scope == "general":
        return True
    return bool(audience) and str(owner_uid) == str(audience)


# ─── store 读入口（解决「先 LIMIT 后过滤」的饥饿问题） ──────────


def visible_chronicle(
    store: Any, scope: str, audience: Optional[str], limit: int = 5
) -> List[Dict[str, Any]]:
    """读取编年史并按受众过滤：多取 → 过滤 → 截断，避免过滤后条目不足。"""
    fetch = min(max(1, limit) * _OVERFETCH, 50)  # 50 = store.recent_chronicle 的上限
    entries = store.recent_chronicle(scope, limit=fetch)
    return filter_entries(entries, audience, limit=limit)


def visible_events(
    store: Any, scope: str, audience: Optional[str], limit: int = 20
) -> List[Dict[str, Any]]:
    """读取事件队列并按受众过滤（同 ``visible_chronicle`` 的多取策略）。"""
    fetch = min(max(1, limit) * _OVERFETCH, 100)  # 100 = store.list_events 的上限
    entries = store.list_events(scope, limit=fetch)
    return filter_entries(entries, audience, limit=limit)
