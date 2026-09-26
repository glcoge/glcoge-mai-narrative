"""双向守卫（批 2-C6 / ADR-0002 §9）——入库前 + 注入前各一道 world_rules 合规闸。

守卫分两道，**语义不同**，不可混用：

| 道 | 时机 | 命中动作 | 理由 |
|---|---|---|---|
| **入库前** | 创作产出（生活片段/编年史）落库前 | **丢弃整条产出** + WARN + 计数 | 没入库的东西不能让别的用户读到；也不能改写（改写＝代码替模型撒谎） |
| **注入前** | 条目送进 LLM prompt 前 | **只丢该条目**，不丢整段 | 注入段还有状态/关系/由头，丢整段会让本轮注入变空 |

关键词来源三路（E6 裁决，见 ``test_continuity_schema.py`` 已锁的 ``guard_keywords``）：
``[identity].world_rules`` + ``[identity].values`` 自动抽取、``[anchor].guard_keywords``
手工补充。本用例锁的是**消费侧**：抽取函数的产物如何被两道闸使用。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load  # noqa: E402

_GUARD = load("services.state.continuity")

build_guard_keywords = _GUARD.build_guard_keywords
guard_violations = _GUARD.guard_violations
guard_blocks_entry = _GUARD.guard_blocks_entry
should_drop_output = _GUARD.should_drop_output
filter_guarded_entries = _GUARD.filter_guarded_entries


# ─── 关键词装配：三路齐全（配置 → 集合） ──────────────────────


class _Identity:
    def __init__(self, world="", world_rules=(), values=(), guard_fragments=()):
        self.world = world
        self.world_rules = list(world_rules)
        self.values = list(values)
        self.guard_fragments = list(guard_fragments)


class _Anchor:
    def __init__(self, guard_keywords=()):
        self.guard_keywords = list(guard_keywords)


class _Config:
    def __init__(self, identity=None, anchor=None):
        self.identity = identity if identity is not None else _Identity()
        self.anchor = anchor if anchor is not None else _Anchor()


def test_build_guard_keywords_merges_three_sources():
    """三路来源全部进集合：world_rules 自动 + values 自动 + 手工补充 ×2 段。"""
    keywords = build_guard_keywords(
        _Config(
            identity=_Identity(
                world_rules=["不可说谎"],
                values=["诚实"],
                guard_fragments=["内部代号"],
            ),
            anchor=_Anchor(guard_keywords=["真实姓名"]),
        )
    )
    assert "不可说谎" in keywords
    assert "诚实" in keywords
    assert "内部代号" in keywords
    assert "真实姓名" in keywords


def test_build_guard_keywords_empty_when_config_bare():
    """什么都没配 → 空集（守卫恒放行，零误伤；不是"守卫失效"）。"""
    assert build_guard_keywords(_Config()) == set()


def test_build_guard_keywords_tolerates_missing_sections():
    """配置对象缺 identity/anchor 段时不炸（老配置 / 测试假件）。"""

    class _Bare:
        pass

    assert build_guard_keywords(_Bare()) == set()


def test_build_guard_keywords_tolerates_missing_guard_fragments():
    """``[identity]`` 没有 guard_fragments 字段（旧配置）时只取另外两路。"""
    keywords = build_guard_keywords(_Config(identity=_Identity(world_rules=["不可说谎"])))
    assert keywords == {"不可说谎", "说谎"}


# ─── 命中判定 ────────────────────────────────────────────────


def test_guard_violations_returns_hits_in_stable_order():
    """命中返回**排序后**的列表：日志/测试断言可靠，不受 set 迭代顺序影响。"""
    hits = guard_violations("这里有内部代号和真实姓名", {"真实姓名", "内部代号", "未命中"})
    assert hits == ["内部代号", "真实姓名"]


def test_guard_violations_empty_when_clean():
    assert guard_violations("今天天气不错", {"内部代号"}) == []


def test_guard_violations_empty_keywords_always_clean():
    """空关键词集恒不命中——守卫默认关闭时不影响任何产出。"""
    assert guard_violations("任何文本", set()) == []


def test_guard_violations_ignores_empty_text():
    assert guard_violations("", {"内部代号"}) == []


def test_guard_blocks_entry_checks_text_field():
    assert guard_blocks_entry({"text": "这里有内部代号"}, {"内部代号"})
    assert not guard_blocks_entry({"text": "干净的内容"}, {"内部代号"})


def test_guard_blocks_entry_tolerates_missing_text():
    """条目无 text / text 非字符串时不炸（fail-open：结构异常交给别的层暴露）。"""
    assert not guard_blocks_entry({}, {"内部代号"})
    assert not guard_blocks_entry({"text": None}, {"内部代号"})
    assert not guard_blocks_entry({"text": 123}, {"内部代号"})


# ─── 第一道：入库前（丢弃整条产出） ─────────────────────────


def test_should_drop_output_drops_on_hit():
    assert should_drop_output("这段写了内部代号", {"内部代号"})


def test_should_drop_output_keeps_clean_output():
    assert not should_drop_output("今天做了顿饭", {"内部代号"})


def test_should_drop_output_keeps_empty_output():
    """空产出不是"违禁"，是"没生成"——由调用方原样 return，不由守卫冒充。"""
    assert not should_drop_output("", {"内部代号"})


# ─── 第二道：注入前（只丢条目，不丢整段） ───────────────────


def test_filter_guarded_entries_drops_only_hitting_entries():
    entries = [
        {"text": "干净的一条"},
        {"text": "夹带内部代号的一条"},
        {"text": "又一条干净的"},
    ]
    kept = filter_guarded_entries(entries, {"内部代号"})
    assert [item["text"] for item in kept] == ["干净的一条", "又一条干净的"]


def test_filter_guarded_entries_preserves_order_and_returns_new_list():
    """不改原序列（注入路径可能同时被别的过滤消费，就地改会串味）。"""
    entries = [{"text": "干净"}, {"text": "内部代号"}]
    kept = filter_guarded_entries(entries, {"内部代号"})
    assert len(entries) == 2
    assert kept is not entries


def test_filter_guarded_entries_no_keywords_passes_all():
    entries = [{"text": "a"}, {"text": "b"}]
    assert filter_guarded_entries(entries, set()) == entries


def test_filter_guarded_entries_all_blocked_yields_empty():
    """全被拦时返回空列表——调用方据此跳过该行，而不是渲染出空壳。"""
    entries = [{"text": "内部代号"}, {"text": "真实姓名"}]
    assert filter_guarded_entries(entries, {"内部代号", "真实姓名"}) == []


# ─── 注入前闸门（渲染层接入口） ──────────────────────────────


_RENDER = load("services.render.planner_block")


def _plugin(world_rules=(), values=(), guard_fragments=(), guard_keywords=()):
    from types import SimpleNamespace

    return SimpleNamespace(
        config=SimpleNamespace(
            identity=_Identity(
                world="",
                world_rules=world_rules,
                values=values,
                guard_fragments=guard_fragments,
            ),
            anchor=_Anchor(guard_keywords=guard_keywords),
            narrative=SimpleNamespace(
                sleep_time="",
                wake_time="",
                sleep_delay_max_minutes=60,
                sleep_delay_recent_minutes=10,
                woken_awake_minutes=30,
                energy_woken_penalty=0.08,
                energy_woken_floor=0.3,
                wake_fragment_enabled=False,
                sleep_pre_sleep_hint_minutes=25,
            ),
        ),
        ctx=SimpleNamespace(
            logger=SimpleNamespace(
                info=lambda *a, **k: None,
                debug=lambda *a, **k: None,
                warning=lambda *a, **k: None,
                error=lambda *a, **k: None,
            )
        ),
    )


def _state(pending_events=()):
    return {
        "identity": {},
        "state": {
            "mood": {"label": "平静", "energy": 0.6, "last_shift_ts": ""},
            "routine": {"phase": "下午", "sleep_time": "", "wake_time": ""},
            "schedule": [],
            "focus": {"pending_events": list(pending_events)},
            "last_interaction_ts": "",
        },
        "chronicle": {"entries": []},
    }


def _branch(milestones=()):
    return {
        "identity": {},
        "relationship": {
            "trust": 0.0,
            "closeness": 0.0,
            "boundaries": 0.0,
            "stage": "相识",
            "first_met": "2026-09-01T10:00:00",
            "milestones": list(milestones),
        },
        "state": {"last_interaction_ts": "", "interaction_count": 0},
        "meta": {"version": 2, "updated_ts": ""},
    }


def _render(plugin, *, entries=(), pending=(), milestones=()):
    import datetime

    return _RENDER.build_context_block(
        plugin,
        _state(pending),
        _branch(milestones),
        datetime.datetime(2026, 9, 22, 11, 37),
        recent_entries=list(entries),
        round_kind="reply",
        audience="123",
    )


def test_inject_guard_drops_hitting_chronicle_entry_only():
    """注入前闸：命中条目被丢，同段的其他条目照常渲染。

    ⚠️ 关键词刻意走 ``guard_fragments``（**只进守卫、不进渲染**）：``world_rules`` /
    ``values`` 会被原样渲染进锚定层行，用它们做断言会和锚定层自身撞车，测不出
    "条目被丢"这件事。守卫不拦锚定层行本身（见 planner_block 的注释）。
    """
    text = _render(
        _plugin(guard_fragments=["言行不一"]),
        entries=[
            {"text": "昨天去海边走了走", "kind": "life", "source_uid": ""},
            {"text": "今天他有点言行不一", "kind": "life", "source_uid": ""},
        ],
    )
    assert "昨天去海边走了走" in text
    assert "言行不一" not in text


def test_inject_guard_drops_hitting_pending_fragment_only():
    """心里挂念的生活片段同样过闸（同一段产出在注入侧再被拦一次）。"""
    text = _render(
        _plugin(guard_fragments=["言行不一"]),
        pending=[
            {"ts": "2026-09-22T10:00:00", "text": "今天在厨房煮了粥", "tier": "minor"},
            {"ts": "2026-09-22T10:30:00", "text": "忽然想起他有点言行不一", "tier": "minor"},
        ],
    )
    assert "今天在厨房煮了粥" in text
    assert "言行不一" not in text


def test_inject_guard_zero_keywords_renders_everything():
    """没配关键词 → 注入内容与守卫上线前完全一致（零行为变更）。"""
    text = _render(
        _plugin(),
        entries=[{"text": "昨天去海边走了走", "kind": "life", "source_uid": ""}],
        pending=[{"ts": "2026-09-22T10:00:00", "text": "煮了粥", "tier": "minor"}],
    )
    assert "昨天去海边走了走" in text
    assert "煮了粥" in text


def test_inject_guard_keyword_come_from_values_too():
    """values 自动抽取的关键词同样生效（不只有 world_rules）。

    措辞刻意避开「诚实守信」这类会原文出现在**锚定层行**的词——锚定层是守卫
    关键词的来源，不受守卫管辖（见 planner_block 的注释），断言只能盯素材行。
    """
    text = _render(
        _plugin(values=["诚实守信"]),
        entries=[{"text": "今天跟朋友聊了很久", "kind": "life", "source_uid": ""}],
    )
    assert "今天跟朋友聊了很久" in text

    # 用 guard_fragments 手工词（不含在 values 原文里）验证 values 侧也进了集合
    text = _render(
        _plugin(values=["诚实守信"], guard_fragments=["言行不一"]),
        entries=[{"text": "他有点言行不一", "kind": "life", "source_uid": ""}],
    )
    assert "言行不一" not in text
