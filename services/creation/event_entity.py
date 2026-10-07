"""事件实体（v0.3.0 批 0 / R41 / 总览 §6.3 「事件因果引用链」）。

把「约定」特例泛化为通用事件实体的**单一实现集中地**：pending_events 条目的
构造、判型、取键只许走本模块（中文语义单一实现铁律同款纪律）。

背景（为什么泛化而不是新建 pending_commitments）：土豆样本（2026-10-01/02）
实证约定机制走通了 pending 路径——约定以自然语言存在片段文本里、由头签发后
由模型从文本「捞」出来呼应。可行，但无结构保证。批 0 给条目补上结构键，
批 2 播种事件（kind=seed）与批 3 建议通道（kind=commitment 的挂靠对象）同池
复用本 schema，由 ``kind`` 分型。

schema（9 字段 + 保留原三键；**缺键 = 该维度不适用**，读取端一律 ``.get``）：

| 键 | fragment | 语义 | 谁来填 |
|---|---|---|---|
| ``event_id`` | 必写 | 全局稳定唯一 id（确定性生成，重放幂等） | 本模块 |
| ``kind`` | 必写 | ``fragment`` / ``commitment`` / ``seed`` | 本模块/后续批次 |
| ``ts`` | 必写 | 产生时刻 ISO 串（秒级）——原键原语义 | life.py |
| ``text`` | 必写 | 呈现文本（由头/注入消费的唯一文本源） | life.py |
| ``highlight`` | 必写 | 高光标记（P19 冻结区，只读不改语义） | life.py |
| ``from_uid`` | 不写键 | 事件发起方（self 语义由 scope 隐含） | 批 3 commitment |
| ``to_uid`` | 不写键 | 事件对象 | 批 3 commitment |
| ``what`` | 不写键 | 结构化概要（谁→谁、做什么；防承诺方向漂移） | 批 3 commitment |
| ``due_ts`` | 不写键 | 履约时限 | 批 3 commitment |
| ``status`` | 不写键 | ``pending`` / ``resolved`` / ``expired``（缺省=pending） | 批 2/3 |
| ``follow_up_of`` | 不写键 | 因果引用（前驱 event_id）——「纠结下楼→买了」延续件 | 批 2/3 |
| ``source_uid`` | 不写键 | 受众隔离标记（audience.py：非空即过滤，fail-closed） | 后续批次 |

向后兼容（零迁移）：旧条目（无 ``kind``/``event_id`` 键）读取时视作
``kind="fragment"``、去复用键回退 ``ts``——新旧同池混存，无迁移脚本、
无 schema_version bump（键增量双向兼容，R12 的 bump 语义是「结构不兼容需重置」）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict

__all__ = [
    "KIND_FRAGMENT",
    "KIND_COMMITMENT",
    "KIND_SEED",
    "DEFAULT_KIND",
    "make_fragment_event",
    "make_seed_event",
    "event_kind",
    "event_key",
    "normalize_event",
]

#: fragment：创作层生活片段（批 0 起 life.py 落库即此型）
KIND_FRAGMENT = "fragment"
#: commitment：跨天约定/承诺（批 3 建议通道的挂靠对象，本批仅登记字面量）
KIND_COMMITMENT = "commitment"
#: seed：世界事件源播种事件（批 2 播种器，本批仅登记字面量）
KIND_SEED = "seed"

#: 旧条目（无 ``kind`` 键）读取时的默认判型——零迁移兼容的单一锚点
DEFAULT_KIND = KIND_FRAGMENT


def _event_id(ts: str, text: str) -> str:
    """确定性生成事件 id：``ev_{ts压缩数字}_{sha1前8位}``。

    同输入（ts, text）恒得同 id——回放台重放、测试重跑均幂等；
    ts 压缩保可读性（人眼能对回库时间），sha1 保区分度（同秒多段不撞）。
    """
    digest = hashlib.sha1(f"{ts}|{text}".encode("utf-8")).hexdigest()[:8]
    compact = re.sub(r"\D", "", ts)
    return f"ev_{compact}_{digest}"


def make_fragment_event(ts: str, text: str, highlight: bool = False) -> Dict[str, Any]:
    """构造一条 fragment 事件实体（life.py 写入端唯一入口）。

    原三键（ts/text/highlight）原语义保留；新增 event_id 与 kind；
    其余维度不写键（缺键 = 不适用，见模块文档 schema 表）。
    """
    # OBSERVE(R41)：本模块 = 事件实体的构造/判型/取键单一实现（登记表 R41 行）。
    return {
        "ts": ts,
        "text": text,
        "highlight": highlight,
        "event_id": _event_id(ts, text),
        "kind": KIND_FRAGMENT,
    }


def make_seed_event(
    ts: str, text: str, importance: str = "low", urgency: str = "short"
) -> Dict[str, Any]:
    """构造一条 seed 事件实体（批 2 / R40，seeder.py 写入端唯一入口）。

    与 fragment 同池同 schema；``highlight`` 恒 False（红线④：播种器与高光签
    两个物种互不触碰）。``importance``/``urgency`` 为 seed 特有元数据
    （HDSI 重要性×时效分类学直抄；本批只登记不消费——「先数据后功能」）。
    """
    # OBSERVE(R40)：seed 落账入口（登记表 R40 行）；元数据默认值与 seeder
    # 降级路径共用「low/short」单一来源。
    return {
        "ts": ts,
        "text": text,
        "highlight": False,
        "event_id": _event_id(ts, text),
        "kind": KIND_SEED,
        "importance": importance,
        "urgency": urgency,
    }


def event_kind(entry: Dict[str, Any]) -> str:
    """判型：显式 ``kind`` 透传，缺键回退 ``fragment``（旧条目零迁移）。"""
    return str(entry.get("kind") or "") or DEFAULT_KIND


def event_key(entry: Dict[str, Any]) -> str:
    """去复用键位：有 ``event_id`` 用之，旧条目回退 ``ts``，两缺则空串。

    空串约定：调用端 ``if key and key in used`` 天然跳过（不登记、不去重），
    与旧代码 ``if ts and ts in used`` 的防御语义一致。
    """
    return str(entry.get("event_id") or "") or str(entry.get("ts") or "")


def normalize_event(entry: Dict[str, Any]) -> Dict[str, Any]:
    """读取端归一化视图：补 ``kind`` 默认值，**不改动原条目**。

    供需要统一形状的消费方（批 2/3 的播种/建议写入端）使用；
    既有消费端（sourcing/planner/status）继续 ``.get`` 直读，无此必要。
    """
    normalized = dict(entry)
    normalized.setdefault("kind", DEFAULT_KIND)
    return normalized
