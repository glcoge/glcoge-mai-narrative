"""证据层（v0.2.0 批 4 · C2）：晋升证据的**代码级**准入与掩码。

本模块是 ADR-0002「证据纪律」与用户 2026-09-26 裁定的唯一落地载体。五条纪律
在此从「文档里的承诺」变成「跑得起来的断言」：

1. **白名单准入**（取代 v1 的黑名单口径）
   证据 eligible ``kind`` ∈ ``GENERAL_KINDS`` = {life, daily}。
   黑名单口径（「只吃 source_uid 空条目」）会把 ``kind=promotion`` 留痕条目放进来——
   晋升历史是**关于她学习状态的模板/AI 输出**，拿它当晋升证据 = **自我强化循环**
   （HDSI 1.10 温水杯）。``diary`` 此前靠 ``SOURCE_DIARY`` 自动打标顺带挡住，
   那是**巧合不是设计**；白名单才闭环。

2. **来源桶掩码**：general 目标（``perspective.world_view``/``life_goals``）
   **代码级丢弃**带 ``source_uid`` 的条目——不得单一来自某私聊/群聊
   （HDSI 5.1「群聊双轨」：私聊内容禁止升格全局）。

3. **只吃正面信号**：relationship 的证据**只有**事件级正向记录（用户主动发起 +
   主动消息承接）。本模块**不提供**任何「互动总数 / 消息条数」读取口——
   把一切互动计数（含吵架）当亲密证据是 HDSI「19:39 抗议」事故的确定性版本。
   ``urge_factor`` 一类**融合值**（被揉过的一维状态）同样不得当证据。

4. **判定单实现**：本模块**不重写**任何窗口/判定逻辑（附加禁令 B）。「用户主动发起」
   的判定已在 ``state/snapshot.Telemetry.is_user_initiated``，且入站 hook 与验收采样
   共用同一份；本模块只**消费已落盘的记录**。

5. **只引用原文 id**：证据引用形如 ``chronicle:<id>``，禁止引用 AI 摘要层
   （HDSI 2.1「推测固化成事实」与 1.10 同源）。

⚠️ 本模块**不 import** ``learning/pairs.py``：配对语义批 4 冻结（R31），
晋升链路与它零耦合（见 ``pytests/test_pairs.py`` 的 AST 断言）。
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from ..render.audience import GENERAL_KINDS
from ..state.continuity import SLOW_FIELD_AUDIENCE

#: 可作晋升证据的编年史 kind（**白名单**）。
#: 直接复用 ``render/audience.GENERAL_KINDS`` —— 中文语义/单实现纪律：同一组 kind
#: 不在两处各写一份。语义差异见模块 docstring 第 1 条（那里管"可见性"，这里管
#: "可作证据"），当前取值相同是**刻意的**，同步性由 test_evidence 的断言锁住。
EVIDENCE_ELIGIBLE_KINDS: FrozenSet[str] = GENERAL_KINDS

#: 认知模式（提案 schema 必带）：区分「她观察到」「她听说」「她相信」——
#: 没有这层标签，``条件`` 会被升格成无条件结论（HDSI 2.1 期待→被爽约）。
COGNITIVE_MODES: Tuple[str, ...] = ("观察", "转述", "信念", "提议", "条件", "确认")

#: 正向信号指标名（csv 落在 ``metrics/<name>.csv``）。
#: ⚠️ 只此两类：① 用户主动发起 ② 主动消息被承接。都是**事件级**记录。
POSITIVE_SIGNAL_METRICS: Tuple[str, ...] = ("user_initiated_freq", "proactive_replied")

#: 来源桶标签。批 1 的 ``source_uid`` 是**隔离标记而非溯源**（不含来源类型），
#: 故代码级只能分「通用 / 受限」两桶——**不假装**能区分私聊与群聊（那需要类型化
#: 标记，登记为后续项；假装能分会导致掩码在细粒度上静默失效）。
BUCKET_GENERAL = "通用"
BUCKET_RESTRICTED = "受限"

#: 自然日切分长度（ISO ``YYYY-MM-DD``）。
_DAY_LEN = 10


def _row_get(row: Any, key: str) -> str:
    """宽松取值（Mapping / sqlite3.Row / 简单对象皆可），统一转字符串。"""
    if isinstance(row, Mapping):
        return str(row.get(key) or "").strip()
    try:
        return str(row[key] or "").strip()
    except (TypeError, KeyError, IndexError):
        return ""


def _kind_of(row: Any) -> str:
    return _row_get(row, "kind")


def _source_of(row: Any) -> str:
    return _row_get(row, "source_uid")


# ─── 白名单准入 ─────────────────────────────────────────────────


def is_evidence_eligible(row: Any) -> bool:
    """该条目是否**允许**进入证据池（白名单）。

    ``kind`` 不在 ``EVIDENCE_ELIGIBLE_KINDS`` 一律 False —— 包括 ``promotion``
    （自我强化循环）与 ``diary``（靠巧合挡住的旧口径，现在靠设计挡住）。
    """
    return _kind_of(row) in EVIDENCE_ELIGIBLE_KINDS


def eligible_entries(rows: Iterable[Any]) -> List[Any]:
    """只做白名单准入（不做来源桶过滤）——供「白名单回归」断言单独使用。"""
    return [row for row in rows if is_evidence_eligible(row)]


def source_bucket(row: Any) -> str:
    """来源桶：``source_uid`` 非空 = 受限素材（私聊或群聊取材，无法进一步区分）。

    与 ``render/audience.py`` 的可见性口径同源：**非空即受限**（fail-closed），
    因此「掩码丢弃受限条目」与「注入侧不让它跨受众」用的是同一个判定，不会分叉。
    """
    return BUCKET_RESTRICTED if _source_of(row) else BUCKET_GENERAL


def mask_evidence(rows: Iterable[Any], *, target: str) -> List[Any]:
    """学习输入掩码：白名单准入 +（general 目标时）来源桶过滤。

    Args:
        rows: 编年史条目（含 ``kind`` / ``source_uid``）。
        target: 慢变目标路径（如 ``perspective.world_view``）。受众规则取自
            ``SLOW_FIELD_AUDIENCE``——**不在这里另写一张表**。

    Raises:
        ValueError: target 未登记（fail-closed：未登记的目标不得有证据通道）。

    general 目标丢弃受限条目是**代码级**的（不是提示词劝阻）：HDSI 已证明
    提示词级不可靠（5.1 群聊双轨事故）。
    """
    normalized = str(target or "").strip()
    audience = SLOW_FIELD_AUDIENCE.get(normalized)
    if audience is None:
        raise ValueError(
            f"未登记的慢变目标：{normalized!r}（不得为其开辟证据通道）"
        )
    result: List[Any] = []
    for row in rows:
        if not is_evidence_eligible(row):
            continue
        if audience == "general" and _source_of(row):
            continue
        result.append(row)
    return result


# ─── 证据引用（只允许原文 id） ──────────────────────────────────


def evidence_ref(row: Any) -> str:
    """构造证据引用 ``chronicle:<id>``。

    缺 ``id`` 时抛 ValueError —— 引用无法定位到原文的"证据"等于没有证据，
    而 AI 摘要层的 id 不在本命名空间内，从格式上就进不来。
    """
    raw_id = _row_get(row, "id")
    if not raw_id:
        raise ValueError("证据条目缺 id，无法构造 chronicle:<id> 引用")
    return f"chronicle:{raw_id}"


def is_valid_cognitive_mode(mode: str) -> bool:
    """认知模式是否在登记枚举内（``条件``/``提议`` 不得升格为无条件结论）。"""
    return str(mode or "").strip() in COGNITIVE_MODES


# ─── 正向信号（只吃事件级记录） ─────────────────────────────────


def positive_signal_days(
    store: Any, uid: str, *, since: Optional[str] = None
) -> Set[str]:
    """返回该用户的**正向场景日**集合（``YYYY-MM-DD``，已按日去重）。

    数据源：``metrics/user_initiated_freq.csv`` + ``metrics/proactive_replied.csv``
    （两者均按「用户 × 事件级 ts」落盘，见批 4 方案 §0.4 核事实）。

    三条刻意的实现约束：

    1. **只用行数、不读 value**：``proactive_replied`` 的 value 语义跨版本变过
       （v0.1.x 记 1.0，v0.2 记延迟分钟）→ 读它就会悄悄改变量纲。本函数只用
       「这一行存在 = 那天有过一次正向事件」。
    2. **按自然日去重**：真实数据里去重比最高 18.56（167 条事件 → 9 天），
       不去重则一天即可刷满门槛（批 4 方案 §0.4 结论 2）。
    3. **不重写判定**：本函数不做任何窗口判定，只消费 ``Telemetry`` 已落盘的记录
       （附加禁令 B：判定逻辑单实现）。
    """
    uid_text = str(uid or "").strip()
    if not uid_text:
        return set()
    since_text = str(since or "").strip()
    days: Set[str] = set()
    for name in POSITIVE_SIGNAL_METRICS:
        rows = store.read_metrics(name) or []
        for row in rows:
            if _row_get(row, "user_id") != uid_text:
                continue
            ts = _row_get(row, "ts")
            day = ts[:_DAY_LEN]
            if len(day) != _DAY_LEN:
                continue
            if since_text and day < since_text:
                continue
            days.add(day)
    return days


def positive_signal_count(store: Any, uid: str, *, since: Optional[str] = None) -> int:
    """正向场景日**数量**（relationship 确定性晋升的直接输入）。"""
    return len(positive_signal_days(store, uid, since=since))


__all__ = [
    "BUCKET_GENERAL",
    "BUCKET_RESTRICTED",
    "COGNITIVE_MODES",
    "EVIDENCE_ELIGIBLE_KINDS",
    "POSITIVE_SIGNAL_METRICS",
    "eligible_entries",
    "evidence_ref",
    "is_evidence_eligible",
    "is_valid_cognitive_mode",
    "mask_evidence",
    "positive_signal_count",
    "positive_signal_days",
    "source_bucket",
]
