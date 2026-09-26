"""话题偏好确定性累积 + 外部加权（批 3-C5 / ADR-0003 §7，登记表 R16 / P10）。

**为什么需要它**（ADR-0003 §7）：话题偏好的累积以「用户实际接话（回复/追问）的互动
事件」为**主要证据源**；生活片段自身主题**降权**——防自主信息茧房
（她写猫 → 素材全猫 → 对人人讲猫）。

**话题表示（零 LLM，确定性）**：``topic_key`` = 由头/片段文本去掉非字母数字字符后的
前 ``TOPIC_KEY_MAX_LEN`` 字签名。

⚠️ 已知局限（如实登记，不粉饰）：表述差异大的同一话题不会聚合（「今天去看了猫」与
「猫真可爱」是两个 key）。这是「确定性 + 零 LLM + 可离线复现」的代价；升级表示需等
二期 LLM 风格提炼（R1/R3）的四件套前置条件成立后再评估。

**证据落点**（E3-(a) 裁定）：

| 事件 | 权重 | 落点 |
|---|---|---|
| 用户接话（承接主动消息） | ``TOPIC_WEIGHT_USER_REPLY``（P10=1.0） | 发送时置 pending → 接住时结算 |
| 生活片段自身主题 | ``TOPIC_WEIGHT_SELF_FRAGMENT``（0.4） | 片段生成入库后直接加权 |
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: P10：用户接话权重（主要证据源）  # OBSERVE(P10)
TOPIC_WEIGHT_USER_REPLY = 1.0

#: 自身主题权重（降权：防自主信息茧房）  # OBSERVE(P10)
TOPIC_WEIGHT_SELF_FRAGMENT = 0.4

#: 话题签名长度上限（确定性表示，见模块 docstring 的局限说明）
TOPIC_KEY_MAX_LEN = 12

_WEIGHT_PREFIX = "topic:weight:"
_PENDING_KEY = "topic:pending:{uid}"


def topic_key(text: str) -> str:
    """把文本归一化成确定性话题签名（去非字母数字 → 截断）。

    中文汉字 ``str.isalnum()`` 为真，故中文不会被误删；标点、空白、emoji 被剔除。
    """
    normalized = "".join(ch for ch in str(text or "") if ch.isalnum())
    return normalized[:TOPIC_KEY_MAX_LEN]


def _weight_of(store: Any, key: str) -> float:
    data = store.get_kv(f"{_WEIGHT_PREFIX}{key}") or {}
    try:
        return float(data.get("weight", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _add_weight(store: Any, key: str, weight: float, sample: str = "") -> None:
    """按 key 累加权重（内部实现；``sample`` 只存展示样本便于排查）。"""
    if not key:
        return
    payload: Dict[str, Any] = {"weight": _weight_of(store, key) + float(weight)}
    if sample:
        payload["sample"] = str(sample)[:40]
    store.set_kv(f"{_WEIGHT_PREFIX}{key}", payload)


def reward_topic(store: Any, text: str, weight: float) -> str:
    """按文本主题累加权重；返回命中的 ``topic_key``（无法成键时返回空串）。"""
    key = topic_key(text)
    _add_weight(store, key, weight, sample=str(text or ""))
    return key


def note_pending_topic(store: Any, user_id: str, text: str) -> str:
    """记下「本轮主动开口讲的是什么话题」，等用户接话时归因（E3-(a)）。"""
    key = topic_key(text)
    if not key:
        return ""
    store.set_kv_str(_PENDING_KEY.format(uid=user_id), key)
    return key


def pending_topic(store: Any, user_id: str) -> str:
    """读取尚未归因的 pending 话题 key（无则空串）。"""
    return str(store.get_kv_str(_PENDING_KEY.format(uid=user_id)) or "")


def reward_pending_topic(
    store: Any, user_id: str, weight: float = TOPIC_WEIGHT_USER_REPLY
) -> str:
    """用户接话 → 给 pending 话题加权（主证据源）。返回被加权的话题 key（无则空串）。"""
    key = pending_topic(store, user_id)
    if not key:
        return ""
    _add_weight(store, key, weight, sample=key)
    return key


def topic_weight(store: Any, key: str) -> float:
    """读取某个话题的累计权重。"""
    return _weight_of(store, key)


def top_topics(store: Any, limit: int = 5) -> List[Tuple[str, float]]:
    """按权重降序返回话题榜（同权重按 key 字典序，保证确定性）。"""
    rows = store.get_kv_with_prefix(_WEIGHT_PREFIX)
    items: List[Tuple[str, float]] = []
    for raw_key, data in rows.items():
        key = raw_key[len(_WEIGHT_PREFIX):]
        try:
            items.append((key, float(data.get("weight", 0.0))))
        except (TypeError, ValueError):
            continue
    items.sort(key=lambda pair: (-pair[1], pair[0]))
    return items[: max(0, int(limit))]


__all__ = [
    "TOPIC_WEIGHT_USER_REPLY",
    "TOPIC_WEIGHT_SELF_FRAGMENT",
    "TOPIC_KEY_MAX_LEN",
    "topic_key",
    "reward_topic",
    "note_pending_topic",
    "pending_topic",
    "reward_pending_topic",
    "topic_weight",
    "top_topics",
]
