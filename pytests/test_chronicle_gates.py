"""编年史接线 + 开关 gate 测试（2026-09-16 B1/B2/B4/B5）。

覆盖四项行为变更：

- **B1 编年史接入主循环**：此前 `maybe_daily_chronicle` 是死代码（零调用点），
  接线时同时修了三个问题：
  ① **跨零点补写** —— 旧写法 ``current.time() < trigger → return``，而 tick 间隔
     30 分钟。若触发点 23:30 而 tick 落在 00:00，判定永不成立 → 功能永不执行。
  ② **幂等键统一** —— 旧代码手写 ``chronicle:done:{today}``，与 store 标准方法
     用的 ``chronicle:{scope}:{kind}:{date}`` 是两套并存。
  ③ 素材范围跟随目标日期（总结哪天取哪天）。
- **B2** `chronicle_enabled` 也管生活片段写入编年史（但生活片段照常生成）。
- **B4** 表达学习隔离绑定开关（任一关闭即放行，不是 abort）。
- **B5** `record_branch_feedback` 补开关判断（此前无 gate，关系值照涨）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_chronicle_gates.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_chronicle_gates.py
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_ENGINE = _synth_loader.load("services.engine")
# plugin.py 依赖 `from .services import ...`，必须先执行 services/__init__.py
_SYNTH_SERVICES = _synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

NarrativeEngine = _ENGINE.NarrativeEngine
MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin

_SELF_SCOPE = "self"


class _Logger:
    """吞掉所有日志的假 logger。"""

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeStore:
    """只实现编年史/事件相关的假 store。

    ``get_kv_int`` / ``set_kv_int`` 故意直接报错 —— B1 已把幂等统一到
    ``is_chronicle_done`` / ``mark_chronicle_done``，若代码回退到手写的
    ``chronicle:done:{date}`` kv 键，这里会立刻红。
    """

    def __init__(self, events=None):
        self.chronicle: list = []
        self.done: set = set()
        self.events: list = events or []
        self.kv_int: dict = {}
        self.kv_str: dict = {}

    def is_chronicle_done(self, scope, kind, date):
        return (scope, kind, date) in self.done

    def mark_chronicle_done(self, scope, kind, date):
        self.done.add((scope, kind, date))

    def append_chronicle(self, scope, kind, text, ts=None):
        self.chronicle.append({"scope": scope, "kind": kind, "text": text, "ts": ts})

    def list_events(self, scope, limit=20):
        return self.events[:limit]

    def get_kv_int(self, key):
        if key.startswith("chronicle:done:"):
            raise AssertionError(f"不应再使用手写的幂等 kv 键: {key}")
        return int(self.kv_int.get(key, 0))

    def set_kv_int(self, key, value):
        if key.startswith("chronicle:done:"):
            raise AssertionError(f"不应再使用手写的幂等 kv 键: {key}")
        self.kv_int[key] = value

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value


class _FakeCreator:
    def __init__(self, text="今天过得还不错。"):
        self.text = text
        self.calls: list = []

    async def generate(self, prompt):
        self.calls.append(prompt)
        return self.text


def _make_engine(
    *,
    plugin_enabled=True,
    narrative_enabled=True,
    chronicle_enabled=True,
    trigger="23:30",
    talk_date="2026-09-15",
    events=None,
    creator=None,
):
    """构造只含编年史链路所需依赖的 engine。"""
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=plugin_enabled),
        narrative=SimpleNamespace(
            enabled=narrative_enabled,
            chronicle_enabled=chronicle_enabled,
            daily_chronicle_time=trigger,
            mode_user_ids=["10001"],
            life_fragment_daily_max=3,
            life_fragment_interval_minutes=240,
        ),
        llm=SimpleNamespace(show_prompt=False),
        identity=SimpleNamespace(world="一座海边小城", values=[], world_rules=[], immutable_traits=[]),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(
        config=cfg, ctx=SimpleNamespace(logger=_Logger())
    )
    engine._store = _FakeStore(events=events)
    engine._creator = creator or _FakeCreator()
    engine._self_state = {
        "state": {
            "last_talk_date": talk_date,
            "mood": {"label": "平静", "energy": 0.6},
            "routine": {"phase": "白天"},
            "focus": {},
        }
    }
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    return engine


# ===== B1：日期归属（跨零点补写） =====


def test_writes_target_day_when_past_trigger():
    """23:40 跑 tick、触发点 23:30 → 总结当天，写入 kind=daily。"""
    engine = _make_engine()
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    assert len(engine._store.chronicle) == 1
    entry = engine._store.chronicle[0]
    assert entry["kind"] == "daily"
    assert entry["ts"].startswith("2026-09-15")


def test_backfills_yesterday_after_midnight():
    """00:10 跑 tick → 补写昨天（旧写法这里会直接 return，功能永不执行）。"""
    engine = _make_engine()
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 16, 0, 10)))
    assert len(engine._store.chronicle) == 1
    assert engine._store.chronicle[0]["ts"].startswith("2026-09-15")
    assert ("self", "daily", "2026-09-15") in engine._store.done


def test_backfill_uses_yesterday_events_only():
    """补写昨天时，素材只取昨天的事件。"""
    events = [
        {"ts": "2026-09-15T10:00:00", "bysource": "昨天的事"},
        {"ts": "2026-09-16T00:05:00", "bysource": "今天的事"},
    ]
    engine = _make_engine(events=events)
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 16, 0, 10)))
    prompt = engine._creator.calls[0]
    assert "昨天的事" in prompt
    assert "今天的事" not in prompt


def test_prompt_date_matches_target_day():
    """prompt 里的日期必须是被总结那天，不是运行时刻。"""
    engine = _make_engine()
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 16, 0, 10)))
    assert "2026-09-15" in engine._creator.calls[0]
    assert "2026-09-16" not in engine._creator.calls[0]


def test_skips_when_target_day_not_reached():
    """当天 20:00、触发点 23:30 → 目标日算作昨天；昨天没说过话就不写。"""
    engine = _make_engine(talk_date="2026-09-15")
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 20, 0)))
    assert engine._store.chronicle == []


# ===== B1：幂等 =====


def test_idempotent_per_day():
    """同一天只写一次（幂等走 store 标准键）。"""
    engine = _make_engine()
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 59)))
    assert len(engine._store.chronicle) == 1
    assert len(engine._creator.calls) == 1


def test_does_not_use_legacy_kv_key():
    """若代码回退到手写 kv 键，_FakeStore 会抛 AssertionError → 此用例变红。"""
    engine = _make_engine()
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    assert not any(k.startswith("chronicle:done:") for k in engine._store.kv_int)


# ===== B1：闸门 =====


def test_skips_when_no_talk_on_target_day():
    engine = _make_engine(talk_date="2026-09-14")
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    assert engine._store.chronicle == []


def test_skips_when_chronicle_disabled():
    engine = _make_engine(chronicle_enabled=False)
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    assert engine._store.chronicle == []


def test_skips_when_plugin_disabled():
    engine = _make_engine(plugin_enabled=False)
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    assert engine._store.chronicle == []


def test_skips_when_narrative_disabled():
    engine = _make_engine(narrative_enabled=False)
    asyncio.run(engine.maybe_daily_chronicle(datetime.datetime(2026, 9, 15, 23, 40)))
    assert engine._store.chronicle == []


# ===== B2：chronicle_enabled 管到生活片段写入 =====


def _make_life_engine(chronicle_enabled):
    engine = _make_engine(chronicle_enabled=chronicle_enabled)
    engine._store.kv_int["life_fragment:count:2026-09-15"] = 0
    engine._store.kv_str["life_fragment:last_ts"] = ""
    engine._load_native_personality = _make_async("")
    return engine


def _make_async(value):
    async def _inner(*a, **k):
        return value
    return _inner


def test_life_fragment_writes_chronicle_when_enabled():
    engine = _make_life_engine(chronicle_enabled=True)
    asyncio.run(engine.maybe_generate_life_fragment(datetime.datetime(2026, 9, 15, 12, 0)))
    kinds = [c["kind"] for c in engine._store.chronicle]
    assert "life" in kinds


def test_life_fragment_skips_chronicle_when_disabled():
    """关闭时生活片段照常生成（pending_events 是主动消息由头），只是不写编年史。"""
    engine = _make_life_engine(chronicle_enabled=False)
    asyncio.run(engine.maybe_generate_life_fragment(datetime.datetime(2026, 9, 15, 12, 0)))
    assert engine._store.chronicle == []
    assert engine._self_state["state"]["focus"]["pending_events"], "生活片段仍应生成"


# ===== B5：record_branch_feedback 受开关控制 =====


def _make_branch_engine(plugin_enabled=True, narrative_enabled=True):
    engine = _make_engine(plugin_enabled=plugin_enabled, narrative_enabled=narrative_enabled)
    branch = {
        "state": {"familiarity": 10.0, "trust": 5.0},
        "identity": {"stage": "陌生人"},
    }
    engine.load_branch_state = lambda uid: branch
    engine.save_branch_state = lambda uid, state: None
    return engine, branch


def test_branch_feedback_advances_when_enabled():
    engine, branch = _make_branch_engine()
    engine.record_branch_feedback("10001", datetime.datetime(2026, 9, 15, 12, 0))
    assert branch["state"]["familiarity"] > 10.0


def test_branch_feedback_frozen_when_narrative_disabled():
    engine, branch = _make_branch_engine(narrative_enabled=False)
    engine.record_branch_feedback("10001", datetime.datetime(2026, 9, 15, 12, 0))
    assert branch["state"]["familiarity"] == 10.0
    assert branch["state"]["trust"] == 5.0


def test_branch_feedback_frozen_when_plugin_disabled():
    engine, branch = _make_branch_engine(plugin_enabled=False)
    engine.record_branch_feedback("10001", datetime.datetime(2026, 9, 15, 12, 0))
    assert branch["state"]["familiarity"] == 10.0


# ===== B4：表达学习隔离绑定开关 =====


def _make_plugin(plugin_enabled=True, narrative_enabled=True, mode_session=True):
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    # SDK 的 config 是只读 property，直接赋值底层实例（属性名见 maibot_sdk/plugin.py:202）
    plugin._plugin_config_instance = SimpleNamespace(
        plugin=SimpleNamespace(enabled=plugin_enabled),
        narrative=SimpleNamespace(enabled=narrative_enabled),
    )
    plugin._is_mode_session = lambda sid: mode_session
    return plugin


def test_expression_aborts_when_enabled_and_mode_session():
    plugin = _make_plugin()
    out = asyncio.run(plugin.block_expression_select(session_id="s1"))
    assert out["action"] == "abort"
    out = asyncio.run(plugin.block_expression_upsert(session_id="s1"))
    assert out["action"] == "abort"


def test_expression_passes_through_when_narrative_disabled():
    """剧本关闭时隔离应放行（continue），不是继续 abort。"""
    plugin = _make_plugin(narrative_enabled=False)
    out = asyncio.run(plugin.block_expression_select(session_id="s1"))
    assert out["action"] == "continue"
    out = asyncio.run(plugin.block_expression_upsert(session_id="s1"))
    assert out["action"] == "continue"


def test_expression_passes_through_when_plugin_disabled():
    plugin = _make_plugin(plugin_enabled=False)
    assert asyncio.run(plugin.block_expression_select(session_id="s1"))["action"] == "continue"
    assert asyncio.run(plugin.block_expression_upsert(session_id="s1"))["action"] == "continue"


def test_expression_passes_through_for_non_mode_session():
    """非剧本会话不干预（与开关无关）。"""
    plugin = _make_plugin(mode_session=False)
    assert asyncio.run(plugin.block_expression_select(session_id="s1"))["action"] == "continue"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
