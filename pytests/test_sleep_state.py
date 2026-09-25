"""睡眠态测试（v0.1.10）。

背景（2026-09-22 用户实测反馈）：内测期间 bot 每 6h 固定生成一段生活片段
（包含深夜），于是日记里天天出现「深夜了还醒着」——因为创作层只有「间隔 +
日上限」两道闸门，**没有睡眠闸门**，而 prompt 只说「你处于深夜」。

本次定案（grill 两轮后的共识）：
- 作息真源 = ``[narrative].sleep_time / wake_time`` 配置；自我层不再存同名键
  （v0.1 遗留的死字段已删）。``sleep_time`` 留空 = 关闭整个睡眠态。
- 睡着 → 不生成生活片段、不主动开口、每 tick 回精力。
- 深夜收到消息 → 「被吵醒」：**瞬时**，不改 sleep_state，只记时刻 + 计数 + 扣精力。
- 醒来 → 补一段「刚醒」的生活片段（豁免间隔闸门与日上限）。
- 入睡推迟：到点仍在聊就推迟，最多 ``sleep_delay_max_minutes``，超时强制入睡。
- 精力恢复判定由「深夜相位」改为「真的睡着且没被吵醒」。
- 失眠**不做**独立建模：信号只认「深夜被吵醒」这一条，不引入随机或规则推导
  （那些信号未经验证，贸然加会多一层回退风险）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_sleep_state.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_sleep_state.py
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_synth_loader.load("services")  # plugin.py 依赖 services/__init__ 的再导出
_ENGINE = _synth_loader.load("services.state.engine")
_RENDER = _synth_loader.load("services.render.planner_block")
_PLUGIN = _synth_loader.load("plugin")

NarrativeEngine = _ENGINE.NarrativeEngine
in_sleep_window = _ENGINE.in_sleep_window
default_self_state = _ENGINE.default_self_state
build_sleep_hint = _RENDER.build_sleep_hint
build_context_block = _RENDER.build_context_block
_sleep_status_line = _PLUGIN._sleep_status_line

_LOGGER = _synth_loader.null_logger()


# ===== 构造工具 =====


def _at(hour: int, minute: int = 0, day: int = 21) -> datetime.datetime:
    return datetime.datetime(2026, 9, day, hour, minute, 0)


class _FakeStore:
    """只提供睡眠态链路所需的最小 store 接口。"""

    def __init__(self, events=None):
        self.events = events or []
        self.kv: dict = {}
        self.kv_int: dict = {}
        self.kv_str: dict = {}
        self.chronicle: list = []
        self.done: set = set()

    def get_kv(self, scope):
        return self.kv.get(scope)

    def set_kv(self, scope, value):
        self.kv[scope] = value

    def get_kv_int(self, key):
        return int(self.kv_int.get(key, 0))

    def set_kv_int(self, key, value):
        self.kv_int[key] = value

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value

    def list_events(self, scope, limit=20):
        return self.events[:limit]

    def append_chronicle(self, scope, kind, text, ts):
        self.chronicle.append((scope, kind, text, ts))

    def is_chronicle_done(self, scope, kind, date_text):
        return (scope, kind, date_text) in self.done

    def mark_chronicle_done(self, scope, kind, date_text):
        self.done.add((scope, kind, date_text))

    def recent_chronicle(self, scope, limit=3):
        return []


class _FakeCreator:
    def __init__(self, text="今天也平常。"):
        self.text = text
        self.prompts: list = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.text


def _make_engine(
    *,
    sleep_time: str = "23:30",
    wake_time: str = "07:00",
    store=None,
    creator=None,
    self_state=None,
    daily_max: int = 16,
    interval: int = 60,
    **sleep_overrides,
) -> NarrativeEngine:
    """构造可跑完整 tick 链路的 engine（含 store / creator / ctx.config）。"""
    narrative = dict(
        enabled=True,
        timezone_offset_hours=8,
        clock_tick_minutes=30,
        daily_chronicle_time="23:30",
        chronicle_enabled=True,
        mode_user_ids=[],
        life_fragment_daily_max=daily_max,
        life_fragment_interval_minutes=interval,
        life_fragment_detail_enabled=False,
        energy_baseline=0.45,
        energy_baseline_pull=0.3,
        energy_sleep_recovery=0.1,
        energy_interaction_boost=0.12,
    )
    narrative.update(_synth_loader.SLEEP_DEFAULTS)
    narrative.update(sleep_time=sleep_time, wake_time=wake_time)
    narrative.update(sleep_overrides)
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(**narrative),
        proactive=SimpleNamespace(urge_enabled=False),
        llm=SimpleNamespace(show_prompt=False),
        identity=SimpleNamespace(world="海边小城", values=[], world_rules=[]),
    )

    async def _get(_path, _default=""):
        return ""

    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(
        config=cfg,
        ctx=SimpleNamespace(logger=_LOGGER, config=SimpleNamespace(get=_get)),
    )
    engine._store = store or _FakeStore()
    engine._creator = creator or _FakeCreator()
    if self_state is not None:
        engine._store.set_kv("self", self_state)
    return engine


def _state(**routine_overrides) -> dict:
    state = default_self_state()
    state["state"]["routine"].update(routine_overrides)
    return state


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ===== 1. 睡眠窗口判定 =====


def test_in_sleep_window_crosses_midnight():
    """跨午夜窗口 23:30-07:00：午夜在内、清晨在内、起床点不在、入睡前不在。"""
    assert in_sleep_window(_at(23, 30), "23:30", "07:00") is True
    assert in_sleep_window(_at(0, 30), "23:30", "07:00") is True
    assert in_sleep_window(_at(6, 59), "23:30", "07:00") is True
    assert in_sleep_window(_at(7, 0), "23:30", "07:00") is False
    assert in_sleep_window(_at(23, 29), "23:30", "07:00") is False
    assert in_sleep_window(_at(12, 0), "23:30", "07:00") is False


def test_in_sleep_window_same_day():
    """不跨午夜的窗口（如 01:00-06:00）同样正确。"""
    assert in_sleep_window(_at(3, 0), "01:00", "06:00") is True
    assert in_sleep_window(_at(0, 30), "01:00", "06:00") is False
    assert in_sleep_window(_at(6, 0), "01:00", "06:00") is False


def test_sleep_time_empty_disables_sleep_state():
    """sleep_time 留空 = 关闭整个睡眠态（回 v0.1.9 及以前的全天不睡行为）。"""
    assert in_sleep_window(_at(2, 0), "", "07:00") is False
    assert in_sleep_window(_at(2, 0), "23:30", "") is False
    engine = _make_engine(sleep_time="", wake_time="")
    assert engine._sleep_configured() is False
    state = _state()
    engine._update_sleep_state(state, _at(2, 0))
    assert state["state"]["routine"]["sleep_state"] == "awake"


# ===== 2. 入睡 / 醒来转换 =====


def test_falls_asleep_at_sleep_time():
    """到点入睡：23:30 的 tick 把状态位翻成 asleep 并记录入睡时刻。"""
    engine = _make_engine()
    state = _state()
    engine._update_sleep_state(state, _at(23, 30))
    routine = state["state"]["routine"]
    assert routine["sleep_state"] == "asleep"
    assert routine["asleep_since"].startswith("2026-09-21T23:30")


def test_delays_sleep_when_still_talking():
    """到点仍在聊（最近一次互动在 10 分钟内）→ 推迟入睡，不翻状态位。"""
    engine = _make_engine()
    state = _state()
    state["state"]["last_interaction_ts"] = _at(23, 26).isoformat(timespec="seconds")
    engine._update_sleep_state(state, _at(23, 30))
    routine = state["state"]["routine"]
    assert routine["sleep_state"] == "awake", "仍在聊时不得入睡"
    assert routine["sleep_delayed_ts"].startswith("2026-09-21T23:30")


def test_forced_sleep_after_delay_budget_exhausted():
    """推迟超过上限后强制入睡：否则「聊到天亮」就永远不睡了。"""
    engine = _make_engine(sleep_delay_max_minutes=30)
    state = _state(sleep_delayed_ts=_at(23, 0).isoformat(timespec="seconds"))
    state["state"]["last_interaction_ts"] = _at(23, 26).isoformat(timespec="seconds")
    engine._update_sleep_state(state, _at(23, 40))
    assert state["state"]["routine"]["sleep_state"] == "asleep"


def test_sleep_delay_disabled_sleeps_on_time():
    """sleep_delay_max_minutes=0 时到点即睡，不看是否还在聊。"""
    engine = _make_engine(sleep_delay_max_minutes=0)
    state = _state()
    state["state"]["last_interaction_ts"] = _at(23, 29).isoformat(timespec="seconds")
    engine._update_sleep_state(state, _at(23, 30))
    assert state["state"]["routine"]["sleep_state"] == "asleep"


def test_wakes_at_wake_time_and_flags_wake_fragment():
    """醒来：状态位回 awake、清空被吵醒痕迹、挂「起床补一段」标。"""
    engine = _make_engine()
    state = _state(sleep_state="asleep", woken_count=3, last_woken_ts=_at(3, 0).isoformat())
    engine._update_sleep_state(state, _at(7, 0))
    routine = state["state"]["routine"]
    assert routine["sleep_state"] == "awake"
    assert routine["wake_fragment_pending"] is True
    assert routine["woken_count"] == 0
    assert routine["last_woken_ts"] == ""
    assert routine["asleep_since"] == ""


# ===== 3. 精力规则改判 =====


def test_energy_recovers_while_asleep():
    """睡着才回血：03:00 睡着时精力应上涨。"""
    engine = _make_engine()
    state = _state(sleep_state="asleep")
    state["state"]["mood"]["energy"] = 0.40
    engine._apply_state_rules(state, _at(3, 0))
    assert state["state"]["mood"]["energy"] > 0.40


def test_no_recovery_when_awake_before_sleep_time():
    """23:10 还在醒着（未到入睡点）→ 不回血（旧写法只看相位，这里会错误回血）。

    起点取 energy_baseline（0.45）让基线回归贡献为 0，这样精力变化只可能来自恢复量。
    """
    engine = _make_engine()
    state = _state(sleep_state="awake")
    state["state"]["mood"]["energy"] = 0.45
    engine._apply_state_rules(state, _at(23, 10))
    assert state["state"]["mood"]["energy"] == 0.45, "醒着却回血了"


def test_no_recovery_when_woken():
    """被吵醒期间不回血——醒了就是醒了（同样取基线为起点，隔离回归项）。"""
    engine = _make_engine()
    state = _state(
        sleep_state="asleep", last_woken_ts=_at(2, 55).isoformat(timespec="seconds")
    )
    state["state"]["mood"]["energy"] = 0.45
    engine._apply_state_rules(state, _at(3, 0))
    assert state["state"]["mood"]["energy"] == 0.45


def test_woken_penalty_applied_and_floor_protects():
    """被吵醒扣精力；一夜多次有地板，连环消息不会把精力打穿。"""
    engine = _make_engine(energy_woken_penalty=0.08, energy_woken_floor=0.3)
    state = _state(sleep_state="asleep")
    state["state"]["mood"]["energy"] = 0.30
    engine._apply_woken_penalty(state, _at(2, 0))
    assert state["state"]["mood"]["energy"] == 0.30, "已在地板上不应再跌"
    assert state["state"]["routine"]["woken_count"] == 1

    state["state"]["mood"]["energy"] = 0.60
    engine._apply_woken_penalty(state, _at(3, 0))
    assert abs(float(state["state"]["mood"]["energy"]) - 0.52) < 0.001
    assert state["state"]["routine"]["woken_count"] == 2


def test_woken_does_not_change_sleep_state():
    """被吵醒是瞬时的：不改 sleep_state，bot 仍然算睡着。"""
    engine = _make_engine()
    state = _state(sleep_state="asleep")
    engine._apply_woken_penalty(state, _at(2, 0))
    assert state["state"]["routine"]["sleep_state"] == "asleep"
    assert state["state"]["routine"]["last_woken_ts"].startswith("2026-09-21T02:00")


def test_no_penalty_when_awake():
    """醒着时收到消息不算「被吵醒」，不记时刻也不扣精力。"""
    engine = _make_engine()
    state = _state(sleep_state="awake")
    state["state"]["mood"]["energy"] = 0.60
    engine._apply_woken_penalty(state, _at(2, 0))
    assert state["state"]["routine"]["last_woken_ts"] == ""
    assert state["state"]["mood"]["energy"] == 0.60


# ===== 4. 创作层闸门 =====


def test_no_fragment_while_asleep():
    """睡着不生产生活片段——「深夜还醒着」的根因修复。"""
    creator = _FakeCreator()
    engine = _make_engine(creator=creator, self_state=_state(sleep_state="asleep"))
    _run(engine.maybe_generate_life_fragment(_at(2, 0)))
    assert creator.prompts == [], "睡着时不得调用创作模型"


def test_wake_fragment_bypasses_interval_and_daily_cap():
    """起床补一段：豁免间隔闸门与日上限，且不计入当日计数。"""
    store = _FakeStore()
    store.kv_int["life_fragment:count:2026-09-21"] = 16  # 日上限已满
    store.kv_str["life_fragment:last_ts"] = _at(6, 50).isoformat(timespec="seconds")
    creator = _FakeCreator("刚醒，窗外还是灰的。")
    engine = _make_engine(
        store=store, creator=creator, daily_max=16, interval=60,
        self_state=_state(sleep_state="awake", wake_fragment_pending=True),
    )
    _run(engine.maybe_generate_life_fragment(_at(7, 0)))

    assert len(creator.prompts) == 1, "起床片段必须生成（豁免间隔与日上限）"
    assert "刚睡醒" in creator.prompts[0]
    assert store.kv_int["life_fragment:count:2026-09-21"] == 16, "起床片段不计入日上限"
    assert store.kv_str["life_fragment:last_ts"].startswith("2026-09-21T07:00")
    assert store.chronicle, "起床片段照常进编年史"


def test_wake_fragment_disabled_by_switch():
    """wake_fragment_enabled=False：醒来不额外生成，pending 标被消费掉不残留。

    把 last_ts 设在 10 分钟前，让**普通路径**也被间隔闸门挡住——这样本用例断言的
    才是开关本身，而不是「恰好没到间隔」。
    """
    store = _FakeStore()
    store.kv_str["life_fragment:last_ts"] = _at(6, 50).isoformat(timespec="seconds")
    creator = _FakeCreator()
    engine = _make_engine(
        store=store, creator=creator, interval=60, wake_fragment_enabled=False,
        self_state=_state(sleep_state="awake", wake_fragment_pending=True),
    )
    _run(engine.maybe_generate_life_fragment(_at(7, 0)))
    assert creator.prompts == []
    state = engine.load_self_state()
    assert "wake_fragment_pending" not in state["state"]["routine"]


def test_normal_fragment_still_respects_interval():
    """普通片段仍受间隔闸门约束（起床豁免不得泛化）。"""
    store = _FakeStore()
    store.kv_str["life_fragment:last_ts"] = _at(9, 50).isoformat(timespec="seconds")
    creator = _FakeCreator()
    engine = _make_engine(
        store=store, creator=creator, interval=60,
        self_state=_state(sleep_state="awake"),
    )
    _run(engine.maybe_generate_life_fragment(_at(10, 0)))
    assert creator.prompts == []


def test_pre_sleep_hint_in_prompt():
    """临近入睡时创作 prompt 追加「今天最后一段」（当天片段自然收在困了上）。"""
    creator = _FakeCreator()
    engine = _make_engine(
        creator=creator, sleep_pre_sleep_hint_minutes=25,
        self_state=_state(sleep_state="awake"),
    )
    _run(engine.maybe_generate_life_fragment(_at(23, 10)))
    assert creator.prompts and "最后一段" in creator.prompts[0]


# ===== 5. 编年史：等真正入睡才写 =====


def test_chronicle_waits_until_asleep():
    """睡眠态启用时，没睡着就不写小结（否则会漏掉入睡前的最后一段对话）。"""
    creator = _FakeCreator("今天过得还行。")
    engine = _make_engine(
        creator=creator, self_state=_state(sleep_state="awake"), store=_FakeStore()
    )
    engine._store.kv["self"]["state"]["last_talk_date"] = "2026-09-21"
    _run(engine.maybe_daily_chronicle(_at(23, 30)))
    assert creator.prompts == [], "还没入睡就写了小结"

    engine._store.kv["self"]["state"]["routine"]["sleep_state"] = "asleep"
    _run(engine.maybe_daily_chronicle(_at(23, 30)))
    assert len(creator.prompts) == 1


def test_chronicle_clock_fallback_when_sleep_disabled():
    """睡眠态关闭时保留原时钟触发行为（test_chronicle_gates 覆盖的是这条路径）。"""
    creator = _FakeCreator("今天过得还行。")
    engine = _make_engine(
        creator=creator, sleep_time="", wake_time="",
        self_state=_state(sleep_state="awake"), store=_FakeStore(),
    )
    engine._store.kv["self"]["state"]["last_talk_date"] = "2026-09-21"
    _run(engine.maybe_daily_chronicle(_at(23, 30)))
    assert len(creator.prompts) == 1


# ===== 6. 由头：睡着不出「还不想睡」 =====


def test_bysource_no_late_night_line_when_asleep():
    """睡着时不得签发「夜深了还不想睡」的由头。"""
    engine = _make_engine(
        self_state=_state(phase="深夜", sleep_state="asleep"), store=_FakeStore()
    )
    text = engine.build_bysource("10001", _at(2, 0))
    assert "不想睡" not in text


# ===== 7. 注入三态提示 =====


def _hint(now: datetime.datetime, routine: dict, **cfg_overrides) -> str:
    cfg = _synth_loader.sleep_config(**cfg_overrides)
    plugin = SimpleNamespace(config=SimpleNamespace(narrative=cfg))
    state = _state(**routine)
    return build_sleep_hint(plugin, state, now)


def test_hint_when_woken_at_night():
    assert "吵醒" in _hint(
        _at(2, 5), {"sleep_state": "asleep", "last_woken_ts": _at(2, 0).isoformat()}
    )


def test_no_hint_when_woken_expired():
    assert _hint(
        _at(3, 5), {"sleep_state": "asleep", "last_woken_ts": _at(2, 0).isoformat()}
    ) == "", "超过 woken_awake_minutes 后不应再提示被吵醒"


def test_hint_just_woke_after_wake_time():
    assert "刚醒" in _hint(_at(7, 20), {"sleep_state": "awake"})


def test_hint_pre_sleep():
    assert "困" in _hint(_at(23, 10), {"sleep_state": "awake"})


def test_no_hint_on_plain_afternoon():
    assert _hint(_at(14, 0), {"sleep_state": "awake"}) == ""


def test_context_block_carries_sleep_hint():
    cfg = _synth_loader.sleep_config()
    plugin = SimpleNamespace(
        config=SimpleNamespace(
            narrative=cfg, identity=SimpleNamespace(world="", values=[], world_rules=[])
        )
    )
    state = _state(
        phase="深夜", sleep_state="asleep", last_woken_ts=_at(2, 0).isoformat()
    )
    text = build_context_block(plugin, state, None, _at(2, 5), [])
    assert "吵醒" in text


# ===== 8. status 展示 =====


def test_status_line_shows_asleep_and_woken_count():
    cfg = _synth_loader.sleep_config()
    line = _sleep_status_line(cfg, {"sleep_state": "asleep", "woken_count": 2})
    assert "睡眠中" in line and "被吵醒 2 次" in line and "23:30 - 07:00" in line


def test_status_line_when_unconfigured():
    cfg = _synth_loader.sleep_config(sleep_time="")
    assert "未配置" in _sleep_status_line(cfg, {})


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
