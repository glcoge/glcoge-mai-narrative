"""慢变软倾向的读取端限流与注入（v0.2.0 批 4 · C7 / R28 / P8）。

晋升机把「看法」写进 state 之后，得有人把它**适度**讲给模型听。本单元锁四件事：

- **上限存在**：最多注入 ``[promotion].projection_limit`` 条（默认 2）。
  全塞进去会把「这一轮具体在聊什么」挤出上下文。
- **相关度排序**：字符 bigram Jaccard（复用批 1 ``sourcing.overlap_ratio``），
  与当前场景更相关的排在前面；同分保持登记顺序（确定性）。
- **授权措辞**：段首必须写清「是**倾向**，不是必需反应，不是不变的身份」——
  少了否定句，模型会把倾向当硬性任务执行（HDSI 授权被压缩丢失）。
- **受众 fail-closed**：``perspective.*`` 是 general（谁都能看），
  ``relationship.*`` 是 per_user（只有归属人能看；``audience`` 为空时不出现）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_slow_projection.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_slow_projection.py
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import _synth_loader

_synth_loader.load("services.state.engine")
_RENDER = _synth_loader.load("services.render.planner_block")

build_context_block = _RENDER.build_context_block
build_slow_tendencies = _RENDER.build_slow_tendencies
collect_slow_tendencies = _RENDER.collect_slow_tendencies
SLOW_TENDENCY_LIMIT = _RENDER.SLOW_TENDENCY_LIMIT

_NOW = datetime.datetime(2026, 9, 26, 10, 30, 0)


def _make_plugin(*, values=None, world_rules=None, projection_limit=None) -> SimpleNamespace:
    promotion = SimpleNamespace()
    if projection_limit is not None:
        promotion.projection_limit = projection_limit
    return SimpleNamespace(
        config=SimpleNamespace(
            identity=SimpleNamespace(
                world="赛博朋克沿海城市",
                values=list(values or []),
                world_rules=list(world_rules or []),
                immutable_traits=[],
            ),
            narrative=_synth_loader.sleep_config(sleep_time="", wake_time=""),
            promotion=promotion,
        )
    )


def _make_state(world_view="", goals=()) -> dict:
    return {
        "state": {
            "mood": {"label": "平静", "energy": 0.45, "last_shift_ts": ""},
            "routine": {"phase": "上午", "sleep_state": "awake"},
            "focus": {"pending_events": []},
            "last_interaction_ts": "",
            "last_talk_date": "",
        },
        "perspective": {
            "world_view": world_view,
            "life_goals": list(goals),
            "origin": "seed",
            "updated_ts": "",
        },
    }


def _branch(stage="亲近", **extra) -> dict:
    relationship = {"trust": 0.4, "closeness": 0.4, "boundaries": 0.0, "stage": stage}
    relationship.update(extra)
    return {"identity": {"first_met": ""}, "relationship": relationship}


# ─── collect：受众规则 ──────────────────────────────────────────


def test_collect_general_visible_without_audience():
    """general 维度对任何受众可见（含受众未知）。"""
    items = collect_slow_tendencies(_make_state("她觉得雨天适合发呆", ["想学会游泳"]), None)
    assert [item["path"] for item in items] == [
        "perspective.world_view",
        "perspective.life_goals",
    ]


def test_collect_per_user_hidden_when_audience_empty():
    """**fail-closed**：受众未知时 per_user 一个都不出现。"""
    items = collect_slow_tendencies(_make_state(), _branch(), audience="")
    assert [item["path"] for item in items] == []


def test_collect_per_user_visible_to_owner():
    items = collect_slow_tendencies(_make_state(), _branch("很亲近"), audience="927386371")
    assert [item["path"] for item in items] == ["relationship.stage"]
    assert "很亲近" in items[0]["text"]


def test_collect_never_exposes_float_dimensions():
    """浮点维度（trust/closeness/boundaries）不参与——数字没法当"倾向"表述。"""
    items = collect_slow_tendencies(
        _make_state("看法"), _branch(), audience="927386371"
    )
    assert not any(item["path"].startswith("relationship.trust") for item in items)
    assert not any("0.4" in item["text"] for item in items)


def test_collect_empty_state_yields_nothing():
    assert collect_slow_tendencies(_make_state(), None) == []


# ─── build：限流 / 排序 / 守卫 ──────────────────────────────────


def test_build_respects_default_limit():
    """默认上限 2：3 条候选只出 2 条。"""
    state = _make_state("看法一", ["目标一", "目标二"])
    picked = build_slow_tendencies(_make_plugin(), state, None)
    assert len(picked) == SLOW_TENDENCY_LIMIT == 2


def test_build_limit_from_config():
    """上限读 ``[promotion].projection_limit``（P8 调参点）。"""
    state = _make_state("看法一", ["目标一", "目标二"])
    picked = build_slow_tendencies(_make_plugin(projection_limit=1), state, None)
    assert len(picked) == 1


def test_build_ranks_by_relevance():
    """相关度：与当前场景重叠更高的排前面（bigram Jaccard 口径）。"""
    state = _make_state("她觉得雨天适合发呆", ["想学会游泳"])
    picked = build_slow_tendencies(
        _make_plugin(projection_limit=1),
        state,
        None,
        query="今天下雨了，雨天真好",
    )
    assert picked == ["她觉得雨天适合发呆"]


def test_build_tie_keeps_registration_order():
    """全部零重叠（空 query）时保持登记顺序——**确定性**便于断言与复现。"""
    state = _make_state("看法一", ["目标一", "目标二"])
    assert build_slow_tendencies(_make_plugin(), state, None, query="") == ["看法一", "目标一"]


def test_build_excludes_requested_paths():
    """``exclude`` 排掉已由专门行渲染过的路径（关系阶段不重复渲染）。"""
    state = _make_state("看法一", ["目标一"])
    picked = build_slow_tendencies(
        _make_plugin(),
        state,
        _branch(),
        audience="927386371",
        exclude=("relationship.stage",),
    )
    assert all("关系已到" not in item for item in picked)


def test_build_drops_guarded_items():
    """注入前过守卫（批 2 同一道闸）：命中关键词的条目不得进上下文。"""
    state = _make_state("她觉得说谎也没关系")
    picked = build_slow_tendencies(
        _make_plugin(values=["说谎"]),
        state,
        None,
    )
    assert picked == [], "命中锚定层关键词的倾向必须被拦下"


def test_build_returns_empty_when_no_candidates():
    assert build_slow_tendencies(_make_plugin(), _make_state(), None) == []


# ─── 段首措辞（授权声明，不得被"精简"掉） ──────────────────────


def test_header_declares_tendency_not_requirement():
    header = _RENDER._SLOW_TENDENCY_HEADER
    assert "倾向" in header
    assert "不是必需反应" in header, "缺'不是必需反应'→ 模型会把倾向当硬任务"
    assert "不是不变的身份" in header, "缺'不是不变的身份'→ 倾向会被读成铁律"


def test_header_avoids_hard_wording():
    header = _RENDER._SLOW_TENDENCY_HEADER
    for hard in ("必须", "一定要", "禁止", "务必"):
        assert hard not in header, f"软倾向段不得出现硬性措辞: {hard}"


# ─── 注入块集成 ─────────────────────────────────────────────────


def _render(state, branch=None, *, audience=None, bysource="") -> str:
    return build_context_block(
        _make_plugin(),
        state,
        branch,
        _NOW,
        [],
        round_kind="reply",
        bysource=bysource,
        audience=audience,
    )


def test_context_block_injects_tendency_section():
    text = _render(_make_state("她觉得雨天适合发呆", ["想学会游泳"]))
    assert "倾向" in text
    assert "她觉得雨天适合发呆" in text
    assert "想学会游泳" in text


def test_context_block_tendency_at_most_two_items():
    text = _render(_make_state("看法一", ["目标一", "目标二", "目标三"]))
    section = text.split("倾向")[1]
    assert section.count("    - ") <= 2


def test_context_block_no_tendency_section_when_empty():
    """没有慢变值时不留空壳标题（否则每次注入都带一行噪声）。"""
    text = _render(_make_state())
    assert "倾向" not in text


def test_context_block_does_not_duplicate_relationship_stage():
    """关系阶段只出现一次（由专门的关系行渲染，不进倾向段）。"""
    text = _render(_make_state("看法一"), _branch("很亲近"), audience="927386371")
    assert text.count("很亲近") == 1, "关系阶段被重复渲染"


def test_context_block_hides_per_user_from_other_audience():
    """**受众隔离**：换个人来看，per_user 的关系阶段不得进倾向段。"""
    text = _render(_make_state("看法一"), _branch("很亲近"), audience="")
    assert "关系已到" not in text


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
