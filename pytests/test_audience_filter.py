"""受众过滤规则表测试（v0.2.0 批 1 · C3 / B4，ADR-0004 第 2 层）。

锁定"读取过滤"这一层。为什么必须单独测：规则表散落多处就会漏一处泄露面，
本批的真实事故（09-20 私聊原文 → 09-21 泄露给第三方）正是这么发生的。

覆盖：
- 规则表逐行（diary 完全隔离 / 涉私原文只给本人 / audience 列 / 通用 / 受众未知）
- ``build_context_block`` 的注入侧过滤（编年史 + 生活片段两条路径）
- ``drop_diary`` 只做最低隔离（diary 对外 API 维持现状，R18 / Q1-b）
- ``visible_chronicle`` / ``visible_events`` 的多取策略（过滤后不饥饿）
- 慢变字段口径（D4 预留，fail-closed）

端到端兜底在回放台（``tools/replay/``），本文件锁单元行为。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_audience_filter.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_audience_filter.py
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_synth_loader.load("services.state.engine")  # plugin.py 依赖 services/__init__.py
_AUDIENCE = _synth_loader.load("services.render.audience")
_RENDER = _synth_loader.load("services.render.planner_block")

SOURCE_DIARY = _AUDIENCE.SOURCE_DIARY
is_visible = _AUDIENCE.is_visible
filter_entries = _AUDIENCE.filter_entries
drop_diary = _AUDIENCE.drop_diary
visible_chronicle = _AUDIENCE.visible_chronicle
visible_events = _AUDIENCE.visible_events
is_slow_field_visible = _AUDIENCE.is_slow_field_visible
build_context_block = _RENDER.build_context_block

_A = "111"
_B = "222"
_NOW = datetime.datetime(2026, 9, 22, 11, 37, 0)


def _entry(**kwargs) -> dict:
    base = {"ts": "2026-09-21T10:00:00", "kind": "life", "text": "内容"}
    base.update(kwargs)
    return base


def _plugin() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            identity=SimpleNamespace(world="", values=[], world_rules=[]),
            narrative=_synth_loader.sleep_config(sleep_time="", wake_time=""),
        )
    )


def _state(pending=None) -> dict:
    return {
        "state": {
            "mood": {"label": "平静", "energy": 0.45, "last_shift_ts": ""},
            "routine": {"phase": "上午", "sleep_state": "awake"},
            "focus": {"hot_thread": "", "pending_events": list(pending or [])},
            "last_interaction_ts": "",
            "last_talk_date": "",
        }
    }


class _FakeStore:
    """只实现 visible_* 需要的两个读方法。"""

    def __init__(self, chronicle=None, events=None):
        self.chronicle = list(chronicle or [])
        self.events = list(events or [])
        self.chronicle_calls: list = []

    def recent_chronicle(self, scope, limit=5):
        self.chronicle_calls.append(limit)
        return self.chronicle[:limit]

    def list_events(self, scope, limit=20):
        return self.events[:limit]


# ===== 规则表逐行 =====


def test_diary_kind_never_visible_even_to_owner():
    """diary 产物完全隔离——**连来源者本人都看不到**（ADR-0004 用户裁定）。"""
    entry = _entry(kind="diary", source_uid=SOURCE_DIARY)
    assert not is_visible(entry, _A)
    assert not is_visible(entry, None)


def test_diary_marker_visible_regardless_of_kind():
    """``source_uid == SOURCE_DIARY`` 这条兜底与 kind 无关（写入侧自动打标的保险）。"""
    assert not is_visible(_entry(kind="life", source_uid=SOURCE_DIARY), _A)


def test_private_original_only_for_its_owner():
    """涉私原文：只讲给本人（ADR-0004 表第 4 行）。"""
    entry = _entry(kind="life", source_uid=_A)
    assert is_visible(entry, _A)
    assert not is_visible(entry, _B)


def test_audience_column_restricts():
    """``audience`` 列非空时按该受众过滤。"""
    entry = _entry(audience=_A)
    assert is_visible(entry, _A)
    assert not is_visible(entry, _B)


def test_untagged_entry_is_general():
    """无标签条目＝通用素材，全员可见（含受众未知）。"""
    entry = _entry()
    assert is_visible(entry, _A)
    assert is_visible(entry, _B)
    assert is_visible(entry, None)


def test_unknown_audience_sees_only_general():
    """受众未知 ≠ 放行全部：涉私条目与 audience 标记条目一律不可见。"""
    assert not is_visible(_entry(source_uid=_A), None)
    assert not is_visible(_entry(audience=_A), None)
    assert is_visible(_entry(), None)


def test_creator_output_without_tag_is_general():
    """创作层消化产出默认不带 source_uid → 通用（溯源走 sources 字段，批 4）。"""
    entry = _entry(kind="life", sources=[_A])
    assert is_visible(entry, _B), "sources 是溯源清单，不参与可见性判定"


def test_filter_entries_preserves_order_and_limit():
    entries = [
        _entry(text="通用1"),
        _entry(text="私", source_uid=_A),
        _entry(text="通用2"),
    ]
    assert [e["text"] for e in filter_entries(entries, _B)] == ["通用1", "通用2"]
    assert [e["text"] for e in filter_entries(entries, _A)] == ["通用1", "私", "通用2"]
    assert len(filter_entries(entries, _A, limit=2)) == 2


def test_drop_diary_only_removes_diary():
    """``drop_diary`` 只去 diary，涉私原文保留（R18 / Q1-b：diary API 维持现状）。"""
    entries = [
        _entry(text="日记", kind="diary"),
        _entry(text="私", source_uid=_A),
        _entry(text="通用"),
    ]
    assert [e["text"] for e in drop_diary(entries)] == ["私", "通用"]


# ===== store 读入口：先过滤后截断（不饥饿） =====


def test_visible_chronicle_overfetches_so_limit_is_met():
    """过滤掉的条目不该挤占名额：多取 → 过滤 → 截断。"""
    entries = [_entry(text=f"私{i}", source_uid=_A) for i in range(3)]
    entries += [_entry(text=f"通用{i}") for i in range(3)]
    store = _FakeStore(chronicle=entries)
    got = visible_chronicle(store, "self", _B, limit=3)
    assert [e["text"] for e in got] == ["通用0", "通用1", "通用2"]
    assert store.chronicle_calls[-1] == 9, "应按倍数多取（3×3）以免过滤后不足"


def test_visible_chronicle_drops_diary():
    store = _FakeStore(chronicle=[_entry(kind="diary", text="日记")])
    assert visible_chronicle(store, "self", _A, limit=3) == []


def test_visible_events_filters_by_audience():
    store = _FakeStore(events=[_entry(source_uid=_A, text="私"), _entry(source_uid=_B, text="他的")])
    assert [e["text"] for e in visible_events(store, f"branch:{_B}", _B, 20)] == ["他的"]


# ===== 注入侧（路径 6/7） =====


def test_injected_block_hides_private_chronicle_from_others():
    """注入块：涉私编年史不得出现在其他受众的注入文本里。"""
    entries = [_entry(text="我叫挪小黑", source_uid=_A)]
    text = build_context_block(_plugin(), _state(), None, _NOW, entries, audience=_B)
    assert "挪小黑" not in text


def test_injected_block_shows_private_chronicle_to_owner():
    """同一条涉私素材讲给本人是允许的（否则本人会觉得 bot 忘了）。"""
    entries = [_entry(text="我叫挪小黑", source_uid=_A)]
    text = build_context_block(_plugin(), _state(), None, _NOW, entries, audience=_A)
    assert "挪小黑" in text


def test_injected_block_hides_diary_fragments_everywhere():
    """生活片段里若混入 diary 产物，对**任何人**都不注入。"""
    pending = [{"ts": "2026-09-21T10:00:00", "text": "今天的日记", "kind": "diary"}]
    text = build_context_block(_plugin(), _state(pending), None, _NOW, [], audience=_A)
    assert "今天的日记" not in text


def test_injected_block_keeps_general_fragments():
    """通用生活片段照常注入（过滤不能把正常功能滤掉）。"""
    pending = [{"ts": "2026-09-21T10:00:00", "text": "去海边走了走"}]
    text = build_context_block(_plugin(), _state(pending), None, _NOW, [], audience=_A)
    assert "去海边走了走" in text


def test_injected_block_without_audience_shows_only_general():
    """未传 audience（受众未知）时，注入块只保留通用素材。"""
    entries = [_entry(text="我叫挪小黑", source_uid=_A), _entry(text="通用生活")]
    text = build_context_block(_plugin(), _state(), None, _NOW, entries)
    assert "挪小黑" not in text
    assert "通用生活" in text


# ===== 慢变字段（D4 预留，批 4 接线） =====


def test_slow_field_general_and_per_user():
    assert is_slow_field_visible("world_view", _B)
    assert is_slow_field_visible("life_goals", _B)
    assert is_slow_field_visible("relationship", _A, owner_uid=_A)
    assert not is_slow_field_visible("relationship", _B, owner_uid=_A)


def test_slow_field_unregistered_is_fail_closed():
    """未登记字段一律不可见——新字段必须先登记再读取（泄露比静默消失更难查）。"""
    assert not is_slow_field_visible("some_new_field", _A, owner_uid=_A)


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
