"""话题偏好累积 + 外部加权（批 3-C5 / ADR-0003 §7，R16 / P10）。

锁四件事：
1. **话题签名确定性**（零 LLM）：归一化去标点/空白、截断、空文本不成键；
2. **外部加权**：用户接话（P10=1.0）是主证据源，自身主题（0.4）降权；
3. **pending 归因链**：发送时置 pending → 接住时结算到该话题；
4. **榜单确定性**：权重降序、同权重按 key 字典序（离线回放必须可复现）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load  # noqa: E402

_TOPIC = load("services.learning.topic")

TOPIC_WEIGHT_USER_REPLY = _TOPIC.TOPIC_WEIGHT_USER_REPLY
TOPIC_WEIGHT_SELF_FRAGMENT = _TOPIC.TOPIC_WEIGHT_SELF_FRAGMENT
TOPIC_KEY_MAX_LEN = _TOPIC.TOPIC_KEY_MAX_LEN
topic_key = _TOPIC.topic_key
reward_topic = _TOPIC.reward_topic
note_pending_topic = _TOPIC.note_pending_topic
pending_topic = _TOPIC.pending_topic
reward_pending_topic = _TOPIC.reward_pending_topic
topic_weight = _TOPIC.topic_weight
top_topics = _TOPIC.top_topics


class _FakeStore:
    """内存版 kv（复用真实 store 的 kv 契约）。"""

    def __init__(self) -> None:
        self.kv: dict = {}
        self.kv_str: dict = {}

    def get_kv(self, key):
        return self.kv.get(key)

    def set_kv(self, key, value):
        self.kv[key] = value

    def get_kv_str(self, key, default=""):
        return self.kv_str.get(key, default)

    def set_kv_str(self, key, value):
        self.kv_str[key] = value

    def get_kv_with_prefix(self, prefix):
        return {k: v for k, v in self.kv.items() if k.startswith(prefix)}


# ─── 话题签名 ───────────────────────────────────────────────


def test_topic_key_strips_punctuation_and_space() -> None:
    assert topic_key("今天，去看了猫！") == "今天去看了猫"


def test_topic_key_truncates() -> None:
    long_text = "一二三四五六七八九十十一十二十三"
    assert len(topic_key(long_text)) == TOPIC_KEY_MAX_LEN


def test_topic_key_empty_text() -> None:
    assert topic_key("") == ""
    assert topic_key("   ，。！") == ""


def test_topic_key_is_deterministic() -> None:
    assert topic_key("猫真可爱") == topic_key("猫真可爱")


# ─── 权重常量（P10） ────────────────────────────────────────


def test_weight_constants() -> None:
    """P10 初值：接话 1.0 是主证据源，自身主题 0.4 降权（防信息茧房）。"""
    assert TOPIC_WEIGHT_USER_REPLY == 1.0
    assert TOPIC_WEIGHT_SELF_FRAGMENT == 0.4
    assert TOPIC_WEIGHT_USER_REPLY > TOPIC_WEIGHT_SELF_FRAGMENT


# ─── 累积 ──────────────────────────────────────────────────


def test_reward_accumulates() -> None:
    store = _FakeStore()
    key = reward_topic(store, "今天去看了猫", TOPIC_WEIGHT_USER_REPLY)
    assert key == "今天去看了猫"
    assert topic_weight(store, key) == pytest.approx(1.0)
    reward_topic(store, "今天去看了猫", TOPIC_WEIGHT_SELF_FRAGMENT)
    assert topic_weight(store, key) == pytest.approx(1.4)


def test_reward_empty_text_writes_nothing() -> None:
    store = _FakeStore()
    assert reward_topic(store, "，。！", 1.0) == ""
    assert store.kv == {}


def test_topic_weight_unknown_key_is_zero() -> None:
    assert topic_weight(_FakeStore(), "不存在") == 0.0


# ─── pending 归因链 ─────────────────────────────────────────


def test_pending_roundtrip() -> None:
    store = _FakeStore()
    assert note_pending_topic(store, "u1", "昨晚梦到下雨") == "昨晚梦到下雨"
    assert pending_topic(store, "u1") == "昨晚梦到下雨"


def test_pending_is_per_user() -> None:
    store = _FakeStore()
    note_pending_topic(store, "u1", "话题甲")
    note_pending_topic(store, "u2", "话题乙")
    assert pending_topic(store, "u1") == "话题甲"
    assert pending_topic(store, "u2") == "话题乙"


def test_reward_pending_credits_named_topic() -> None:
    store = _FakeStore()
    note_pending_topic(store, "u1", "昨晚梦到下雨")
    key = reward_pending_topic(store, "u1")
    assert key == "昨晚梦到下雨"
    assert topic_weight(store, "昨晚梦到下雨") == pytest.approx(TOPIC_WEIGHT_USER_REPLY)


def test_reward_pending_without_pending_is_noop() -> None:
    store = _FakeStore()
    assert reward_pending_topic(store, "u1") == ""
    assert store.kv == {}


def test_pending_empty_text_not_stored() -> None:
    store = _FakeStore()
    assert note_pending_topic(store, "u1", "。，,") == ""
    assert pending_topic(store, "u1") == ""


# ─── 榜单 ──────────────────────────────────────────────────


def test_top_topics_sorted_desc() -> None:
    store = _FakeStore()
    reward_topic(store, "话题甲", 0.4)
    reward_topic(store, "话题乙", 1.0)
    reward_topic(store, "话题丙", 2.0)
    assert top_topics(store, 3) == [("话题丙", 2.0), ("话题乙", 1.0), ("话题甲", 0.4)]


def test_top_topics_tie_broken_by_key_order() -> None:
    """同权重按 key 字典序——离线回放必须逐次一致。"""
    store = _FakeStore()
    reward_topic(store, "乙", 1.0)
    reward_topic(store, "甲", 1.0)
    assert [key for key, _ in top_topics(store, 5)] == ["乙", "甲"]


def test_top_topics_limit() -> None:
    store = _FakeStore()
    for index in range(4):
        reward_topic(store, f"话题{index}", 1.0)
    assert len(top_topics(store, 2)) == 2


def test_top_topics_empty_store() -> None:
    assert top_topics(_FakeStore(), 5) == []


def test_top_topics_tolerates_dirty_value() -> None:
    store = _FakeStore()
    store.set_kv("topic:weight:脏", {"weight": "not-a-number"})
    reward_topic(store, "好话题", 1.0)
    assert top_topics(store, 5) == [("好话题", 1.0)]


if __name__ == "__main__":
    from pytests._synth_loader import run_standalone

    raise SystemExit(run_standalone(globals()))
