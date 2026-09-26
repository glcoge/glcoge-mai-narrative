"""生活片段「按事件重要度分级详略」测试（2026-09-21 新增）。

覆盖：
- 三信号定档：① 窗口内新增关系里程碑 ② energy 极端 ③ 素材密度 ≥4 → major
- 无信号但有素材 → normal；无素材 → minor
- `life_fragment_detail_enabled=False` → flat（旧行为，统一 40~90 字）
- prompt 分档：长度区间与素材展示上限随档位变化
- 渲染侧：detail 开启时最新片段放宽到 120 字，关闭时仍 56 字

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_life_fragment_tier.py -q
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_ENGINE = _synth_loader.load("services.state.engine")
_SYNTH_SERVICES = _synth_loader.load("services")
_RENDER = _synth_loader.load("services.render.planner_block")

NarrativeEngine = _ENGINE.NarrativeEngine
build_context_block = _RENDER.build_context_block

_NOW = datetime.datetime(2026, 9, 21, 12, 0, 0)


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeStore:
    """只提供档位判定所需的最小 store 接口。"""

    def __init__(self, events=None):
        self.events = events or []
        self.kv_int: dict = {}
        self.kv_str: dict = {}

    def list_events(self, scope, limit=20):
        return self.events[:limit]

    def get_kv_int(self, key):
        return int(self.kv_int.get(key, 0))

    def set_kv_int(self, key, value):
        self.kv_int[key] = value

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value


def _make_engine(*, detail_enabled=True, energy=0.6, events=None, milestones=None,
                 window_start=datetime.datetime(2026, 9, 21, 10, 0, 0)):
    """构造只含档位判定依赖的 engine。"""
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            chronicle_enabled=True,
            mode_user_ids=["10001"],
            life_fragment_daily_max=6,
            life_fragment_interval_minutes=120,
            life_fragment_detail_enabled=detail_enabled,
            # 2026-09-22：本文件不测睡眠，留空关闭睡眠态（prompt 的临近入睡分支随之跳过）
            sleep_time="",
            wake_time="",
        ),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
        identity=SimpleNamespace(world="一座海边小城", values=[], world_rules=[], immutable_traits=[]),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=_Logger()))
    engine._store = _FakeStore(events=events)
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": energy, "last_shift_ts": ""},
            "routine": {"phase": "白天", "sleep_state": "awake"},
            "focus": {"pending_events": []},
        }
    }
    branch = {
        "relationship": {"stage": "陌生人", "milestones": milestones or [], "first_met": ""},
        "state": {"interaction_count": 0},
    }
    engine.load_branch_state = lambda uid: branch
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    return engine, window_start


def _event(ts: str, text: str = "聊了会儿天气") -> dict:
    return {"ts": ts, "scope": "branch:10001", "kind": "dialogue_material", "bysource": text}


# ===== 三信号定档 =====


def test_flat_when_detail_disabled():
    """关闭分级 → flat（旧行为），即使能量极端也不例外。"""
    engine, ws = _make_engine(detail_enabled=False, energy=0.1)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "flat"


def test_minor_when_no_material():
    """无素材、无信号 → minor。"""
    engine, ws = _make_engine()
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "minor"


def test_normal_with_few_materials():
    """有素材但未达密度阈值 → normal。"""
    events = [_event("2026-09-21T11:00:00"), _event("2026-09-21T11:30:00")]
    engine, ws = _make_engine(events=events)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "normal"


def test_major_on_energy_extreme_low():
    engine, ws = _make_engine(energy=0.2)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "major"


def test_major_on_energy_extreme_high():
    engine, ws = _make_engine(energy=0.95)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "major"


def test_major_on_new_milestone_in_window():
    """窗口内新增里程碑（关系阶段晋升）→ major，即使无素材。"""
    milestones = [{"id": "stage:朋友", "ts": "2026-09-21T11:00:00", "desc": "成为朋友"}]
    engine, ws = _make_engine(milestones=milestones)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "major"


def test_milestone_outside_window_is_not_major():
    """窗口外的里程碑不算信号（历史里程碑不该天天触发 major）。"""
    milestones = [{"id": "stage:熟人", "ts": "2026-09-15T11:00:00", "desc": "成为熟人"}]
    engine, ws = _make_engine(milestones=milestones)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "minor"


def test_major_on_material_density():
    """窗口内素材 ≥4 条 → major。"""
    events = [_event(f"2026-09-21T1{i}:00:00") for i in range(1, 5)]
    engine, ws = _make_engine(events=events)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "major"


def test_events_before_window_not_counted():
    events = [_event("2026-09-21T09:00:00") for _ in range(6)]  # 窗口起点 10:00 之前
    engine, ws = _make_engine(events=events)
    assert engine._life_fragment_tier(_NOW, engine._self_state, ws) == "minor"


# ===== prompt 分档 =====


def _prompt(engine, tier, materials):
    return engine._build_life_fragment_prompt(
        _NOW, engine._self_state, materials, persona="一只小麒麟", tier=tier
    )


def test_prompt_length_ranges_per_tier():
    engine, _ws = _make_engine()
    assert "40~90 字" in _prompt(engine, "minor", [])
    assert "80~180 字" in _prompt(engine, "normal", ["聊了会儿天气"])
    assert "200~400 字" in _prompt(engine, "major", ["聊了会儿天气"])
    assert "40~90 字" in _prompt(engine, "flat", [])


def test_prompt_major_asks_for_detail():
    """major 档必须明确要求写细（起因/感受/细节/认知），而非一笔带过。"""
    engine, _ws = _make_engine()
    text = _prompt(engine, "major", ["聊了会儿天气"])
    for keyword in ("起因", "感受", "细节", "认知"):
        assert keyword in text, f"major 档 prompt 缺「{keyword}」要求"


def test_prompt_material_cap_by_tier():
    """素材展示上限随档位放宽：normal 120 字符、major 300 字符。"""
    engine, _ws = _make_engine()
    long_material = "甲" * 400
    normal_text = _prompt(engine, "normal", [long_material])
    major_text = _prompt(engine, "major", [long_material])
    assert "甲" * 120 in normal_text and "甲" * 121 not in normal_text
    assert "甲" * 300 in major_text and "甲" * 301 not in major_text


# ===== 渲染侧：细节要活到主 prompt =====


def _render_with_fragment(fragment: str, *, detail_enabled: bool) -> str:
    config = SimpleNamespace(
        identity=SimpleNamespace(world="海边小城", values=[], world_rules=[], immutable_traits=[]),
        narrative=SimpleNamespace(
            life_fragment_detail_enabled=detail_enabled,
            # 2026-09-22：build_context_block 现要读睡眠配置；留空=不睡觉，不注入睡眠提示
            sleep_time="",
            wake_time="",
            sleep_pre_sleep_hint_minutes=25,
            woken_awake_minutes=30,
        ),
    )
    plugin = SimpleNamespace(config=config)
    state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.45, "last_shift_ts": ""},
            "routine": {"phase": "上午", "sleep_state": "awake"},
            "focus": {"pending_events": [{"ts": "2026-09-21T11:00:00", "text": fragment}]},
            "last_interaction_ts": "",
            "last_talk_date": "",
        }
    }
    return build_context_block(plugin, state, None, _NOW, [])


def test_render_keeps_full_fragment():
    """major 档片段（如 400 字）应整体进入主 prompt，不再被旧上限截断。"""
    fragment = "乙" * 400
    text = _render_with_fragment(fragment, detail_enabled=True)
    assert ("乙" * 400) in text, "400 字片段不应被截断"
    assert ("乙" * 401) not in text, "原文只有 400 字"


def test_render_hard_cap_prevents_runaway():
    """上限 1024 是防失控天花板（不是调节旋钮）：超长文本仍要被截住。"""
    fragment = "乙" * 5000
    text = _render_with_fragment(fragment, detail_enabled=True)
    assert ("乙" * 1024) in text, "应保留到 1024 字"
    assert ("乙" * 1025) not in text, "超过 1024 字的部分必须截掉"


def test_bysource_keeps_full_fragment():
    """主动开口的由头不再只取前 60 字（旧值会把 major 细节截掉）。"""
    engine, _ws = _make_engine()
    fragment = "甲" * 300
    engine._self_state["state"]["focus"]["pending_events"] = [
        {"ts": "2026-09-21T11:00:00", "text": fragment}
    ]
    bysource = engine.build_bysource("10001", _NOW)
    assert "甲" * 300 in bysource, "由头应带完整片段（旧上限 60 字会截断）"
    assert "甲" * 301 not in bysource


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
