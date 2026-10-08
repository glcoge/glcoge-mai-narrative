"""事件实体（v0.3.0 批 0 / R41 / 总览 §6.3）。

覆盖四层：
- **构造**：``make_fragment_event`` 的形状与幂等（确定性 event_id，重放/回放台安全）；
- **判型/取键**：旧条目（无 ``kind``/``event_id`` 键）零迁移兼容——缺键 = 默认
  ``fragment`` / 回退 ``ts``；
- **可见性**：新条目（不带 ``source_uid``）走 ``is_visible`` 判定 = 通用，
  与旧条目一致（audience 层 fail-closed 语义不被新键破坏）；
- **写入端集成**：life.py 落库条目带 ``event_id`` + ``kind=fragment``
  （text/ts 与旧结构逐字节同语义，零行为 diff）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_event_entity.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from types import SimpleNamespace

import _synth_loader
from pytests._synth_loader import FakeLogger, FakeStore, make_logger  # noqa: E402

_ENTITY = _synth_loader.load("services.creation.event_entity")
_AUDIENCE = _synth_loader.load("services.render.audience")

make_fragment_event = _ENTITY.make_fragment_event
make_seed_event = _ENTITY.make_seed_event
event_kind = _ENTITY.event_kind
event_key = _ENTITY.event_key
normalize_event = _ENTITY.normalize_event
is_visible = _AUDIENCE.is_visible

_TS = "2026-10-07T15:30:00"
_TEXT = "傍晚去江边走了走，风有点凉。"


# ===== 构造 =====


def test_make_fragment_event_shape():
    """形状：原三键（ts/text/highlight）语义不变 + 新增 event_id/kind。"""
    entry = make_fragment_event(ts=_TS, text=_TEXT, highlight=False)
    assert entry["ts"] == _TS
    assert entry["text"] == _TEXT
    assert entry["highlight"] is False
    assert entry["kind"] == "fragment"
    assert entry["event_id"].startswith("ev_")
    # 其余维度（from_uid/to_uid/what/due_ts/status/follow_up_of/source_uid）
    # 批 0 的 fragment 不写键——缺键 = 该维度不适用
    assert "from_uid" not in entry
    assert "to_uid" not in entry
    assert "what" not in entry
    assert "due_ts" not in entry
    assert "status" not in entry
    assert "follow_up_of" not in entry
    assert "source_uid" not in entry


def test_make_fragment_event_idempotent():
    """同输入（ts, text）→ 同 event_id：确定性生成，重放幂等。"""
    first = make_fragment_event(ts=_TS, text=_TEXT, highlight=True)
    second = make_fragment_event(ts=_TS, text=_TEXT, highlight=True)
    assert first["event_id"] == second["event_id"]


def test_event_id_distinguishes_inputs():
    """不同 ts 或不同 text → 不同 event_id（id 不能撞）。"""
    by_ts = make_fragment_event(ts="2026-10-07T15:31:00", text=_TEXT)
    by_text = make_fragment_event(ts=_TS, text="另一段片段")
    base = make_fragment_event(ts=_TS, text=_TEXT)
    assert len({by_ts["event_id"], by_text["event_id"], base["event_id"]}) == 3


# ===== 判型 / 取键（零迁移兼容） =====


def test_event_kind_defaults_to_fragment_for_legacy():
    """旧条目（无 kind 键）读取判型 = fragment——零迁移的核心规则。"""
    legacy = {"ts": _TS, "text": _TEXT, "highlight": False}
    assert event_kind(legacy) == "fragment"


def test_event_kind_passthrough():
    """显式 kind 透传（批 2 seed / 批 3 commitment 的预留路径）。"""
    assert event_kind({"kind": "seed"}) == "seed"
    assert event_kind({"kind": "commitment"}) == "commitment"


def test_event_key_prefers_event_id():
    """新条目取 event_id（稳定 id 作去复用键）。"""
    entry = make_fragment_event(ts=_TS, text=_TEXT)
    assert event_key(entry) == entry["event_id"]


def test_event_key_falls_back_to_ts():
    """旧条目回退 ts——存量 `bysource:used`（ts 集）零迁移直接混用。"""
    legacy = {"ts": _TS, "text": _TEXT}
    assert event_key(legacy) == _TS


def test_event_key_empty_when_neither():
    """两键皆缺 → 空串（调用端 `if key and key in used` 天然跳过）。"""
    assert event_key({"text": _TEXT}) == ""


def test_normalize_event_fills_kind_without_mutation():
    """normalize 补 kind 默认值且不改原条目（读取端归一化视图）。"""
    legacy = {"ts": _TS, "text": _TEXT}
    normalized = normalize_event(legacy)
    assert normalized["kind"] == "fragment"
    assert "kind" not in legacy, "原条目不得被就地修改"


# ===== 可见性（新键不破坏 audience 层） =====


def test_new_entry_visible_as_general():
    """新条目无 source_uid → 通用可见，与旧条目判定一致（fail-closed 不误伤）。"""
    entry = make_fragment_event(ts=_TS, text=_TEXT)
    assert is_visible(entry, None) is True
    assert is_visible(entry, "10001") is True
    legacy = {"ts": _TS, "text": _TEXT}
    assert is_visible(legacy, None) == is_visible(entry, None)


# ===== 写入端集成（life.py 落库即事件实体） =====






class _FakeCreator:
    async def generate(self, prompt):
        return "今天在厨房煮了粥。"


def _make_life_engine():
    """复用 test_fragment_pending_capacity 的装配手法（走 engine 薄委托）。"""
    engine_mod = _synth_loader.load("services.state.engine")
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            chronicle_enabled=True,
            mode_user_ids=["10001"],
            life_fragment_daily_max=16,
            life_fragment_interval_minutes=30,
            life_fragment_detail_enabled=False,
            fragment_pending_max=12,
            highlight_probability=0.0,
            sleep_time="",
            wake_time="",
            wake_fragment_enabled=False,
            sleep_pre_sleep_hint_minutes=25,
        ),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
        identity=SimpleNamespace(world="", values=[], world_rules=[], guard_fragments=[]),
        anchor=SimpleNamespace(guard_keywords=[]),
    )
    engine = engine_mod.NarrativeEngine.__new__(engine_mod.NarrativeEngine)
    engine._plugin = SimpleNamespace(
        config=config, ctx=SimpleNamespace(logger=FakeLogger()), _telemetry=None
    )
    engine._store = FakeStore()
    engine._creator = _FakeCreator()
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6},
            "routine": {"phase": "白天", "sleep_state": "awake"},
            "focus": {"pending_events": []},
        }
    }
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    engine.load_branch_state = lambda uid: {"relationship": {"milestones": []}}
    return engine


def test_life_write_end_produces_event_entity():
    """life.py 落库条目 = 事件实体：event_id + kind=fragment，text/ts 语义不变。"""
    engine = _make_life_engine()
    moment = datetime.datetime(2026, 10, 7, 15, 30, 0)
    asyncio.run(engine.maybe_generate_life_fragment(moment))

    pending = engine._self_state["state"]["focus"]["pending_events"]
    assert len(pending) == 1
    entry = pending[0]
    assert entry["text"] == "今天在厨房煮了粥。"
    assert entry["ts"] == moment.isoformat(timespec="seconds")
    assert entry["highlight"] is False
    assert entry["kind"] == "fragment"
    assert entry["event_id"].startswith("ev_")
    # 编年史双写不受影响（零行为 diff）
    assert engine._store.chronicle[0]["kind"] == "life"
    assert engine._store.chronicle[0]["text"] == "今天在厨房煮了粥。"


# ===== seed 分型（批 2 / R40：世界事件源落账入口） =====


def test_make_seed_event_shape():
    """seed 实体：kind=seed + importance/urgency 元数据透传 + highlight 恒 False。"""
    entry = make_seed_event(ts=_TS, text="巷口的猫生了小猫。", importance="mid", urgency="short")
    assert entry["kind"] == "seed"
    assert entry["text"] == "巷口的猫生了小猫。"
    assert entry["ts"] == _TS
    assert entry["highlight"] is False, "P19 红线④：播种器与高光签两个物种，seed 恒不高光"
    assert entry["event_id"].startswith("ev_")
    assert entry["importance"] == "mid"
    assert entry["urgency"] == "short"


def test_make_seed_event_defaults():
    """元数据缺省 = low/short（JSON 解析失败降级的同一默认，单一来源）。"""
    entry = make_seed_event(ts=_TS, text="某件小事。")
    assert entry["importance"] == "low"
    assert entry["urgency"] == "short"


def test_seed_event_visible_as_general():
    """seed 条目不带 source_uid → 通用可见（与 fragment 一致，进所有会话候选）。"""
    entry = make_seed_event(ts=_TS, text="某件小事。")
    assert is_visible(entry, None) is True
    assert is_visible(entry, "10001") is True


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
