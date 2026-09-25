"""share_urge 分享欲测试（v0.1.8 第一步：动机驱动主动时机，规则层零 LLM）。

设计定案（2026-09-17 用户拍板，基于 issue-share-urge.md 四个开放问题）：
- 两层相乘：self 层基线漂移 × branch 层对人系数 × 精力因子，clamp [0,1]；
- 承接窗口 = 冷落窗口 **16 小时**（2026-09-22 改，原 30min）：实测承接延迟中位
  4.2 小时，30min 窗口会把六成成功承接先判成"冷落"再罚一次，是沉默螺旋的结构性
  来源。窗口内被接住 → +gain；超窗无人接住（且**已确认送达**）→ -decay；
  未送达的不罚，单列 proactive_undelivered；
- 第一步不做峰值即时触发：保留随机计时器作最小间隔闸门，到点后按分享欲采样，
  未过则 30-60 分钟短延迟重试；
- 与 Agency Window 不重复门控：share_urge 只作用于主动开口时机，
  用户主动来找时回复路径绝不设门。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_share_urge.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_share_urge.py
"""

from __future__ import annotations

import asyncio
import copy
import datetime
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import _synth_loader

_ENGINE = _synth_loader.load("services.state.engine")
_PROACTIVE = _synth_loader.load("services.proactive.scheduler")

NarrativeEngine = _ENGINE.NarrativeEngine
ProactiveScheduler = _PROACTIVE.ProactiveScheduler

_NOW = datetime.datetime(2026, 9, 17, 15, 0)  # 周四 15:00：默认窗口内、非静默期
_UID = "10000"


# ===== 构造工具 =====


class _FakeStore:
    """最小 kv 存储：get/set 语义与 NarrativeStore 对齐（返回副本防引用泄漏）。"""

    def __init__(self) -> None:
        self.kv: Dict[str, Any] = {}

    def get_kv(self, key: str) -> Any:
        value = self.kv.get(key)
        return copy.deepcopy(value) if value is not None else None

    def set_kv(self, key: str, value: Any) -> None:
        self.kv[key] = copy.deepcopy(value)

    def get_kv_int(self, key: str) -> int:
        return int(self.kv.get(key, 0) or 0)


class _QuietLogger:
    """吞掉全部日志调用。"""

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:
        pass


def _make_engine(
    *,
    plugin_enabled: bool = True,
    narrative_enabled: bool = True,
    urge_enabled: bool = True,
    urge_base: float = 0.7,
    urge_gain: float = 0.1,
    urge_decay: float = 0.25,
    urge_regain: float = 0.15,
    urge_branch_floor: float = 0.4,
    energy_baseline: float = 0.45,
) -> NarrativeEngine:
    """构造带 fake store 的 engine（record_urge_feedback / compute_share_urge 可直接跑）。"""
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=plugin_enabled),
        narrative=SimpleNamespace(
            enabled=narrative_enabled,
            timezone_offset_hours=8,
            clock_tick_minutes=30,
            energy_baseline=energy_baseline,
            **_synth_loader.SLEEP_DEFAULTS,
        ),
        proactive=SimpleNamespace(
            urge_enabled=urge_enabled,
            urge_base=urge_base,
            urge_gain=urge_gain,
            urge_decay=urge_decay,
            urge_regain=urge_regain,
            urge_branch_floor=urge_branch_floor,
        ),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(
        config=cfg, ctx=SimpleNamespace(logger=_QuietLogger())
    )
    engine._store = _FakeStore()  # engine 自持 store 引用（load/save_self_state 直接访问）
    # 初始化 self state（走真实 default 结构，energy 可控）
    state = engine.load_self_state()
    state["state"]["mood"]["energy"] = energy_baseline  # 默认满因子
    engine.save_self_state(state)
    return engine


def _make_scheduler(
    *,
    urge: float = 1.0,
    urge_enabled: bool = True,
    now: datetime.datetime = _NOW,
):
    """构造最小可跑 _check_once 的 scheduler。

    Returns:
        (scheduler, fired_uids, urge_events)：fired 记录 _fire 调用；
        urge_events 记录 engine 收到的冷落等反馈事件。
    """
    fired: List[str] = []
    urge_events: List[tuple] = []
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(enabled=True, mode_user_ids=[_UID]),
        proactive=SimpleNamespace(
            enabled=True,
            daily_max=5,
            silent_start="04:00",
            silent_end="05:00",
            default_active_window=["00:00-23:59"],
            random_minutes=[60, 240],
            user_window_rules=[],
            urge_enabled=urge_enabled,
            urge_base=0.7,
            urge_gain=0.1,
            urge_decay=0.25,
            urge_regain=0.15,
            urge_branch_floor=0.4,
        ),
    )
    engine = SimpleNamespace(
        # 与真实 compute_share_urge 的 gate 语义一致：总开关关闭 → 返回 1.0（到点必发）
        compute_share_urge=lambda uid: (urge if urge_enabled else 1.0),
        record_urge_feedback=lambda uid, event: urge_events.append((uid, event)),
        # 睡眠闸门（v0.1.10）：本文件测分享欲，时间固定在 15:00，恒清醒
        is_asleep=lambda now=None: False,
    )
    # 指标采集替身：proactive_undelivered / proactive_trigger_failed 由调度循环写入
    metrics: List[Any] = []
    plugin = SimpleNamespace(
        config=cfg,
        _engine=engine,
        _telemetry=SimpleNamespace(
            record=lambda name, value=1, user_id="", scope="": metrics.append(
                (name, value, user_id, scope)
            )
        ),
        _streams=SimpleNamespace(stream_of=lambda uid: f"stream-{uid}"),
        _store=_FakeStore(),
        _local_now=lambda: now,
        ctx=SimpleNamespace(logger=_QuietLogger()),
    )
    scheduler = ProactiveScheduler.__new__(ProactiveScheduler)
    scheduler._plugin = plugin
    scheduler._task = None
    scheduler._running = False
    scheduler._next_fire: Dict[str, Any] = {}
    scheduler._sent_records: Dict[str, Any] = {}
    scheduler._pending_at: Dict[str, Any] = {}
    scheduler._metrics = metrics

    async def fake_fire(user_id: str, stream_id: str, ts: datetime.datetime) -> None:
        fired.append(user_id)

    scheduler._fire = fake_fire
    return scheduler, fired, urge_events


# ===== engine 层：事件反馈 =====


def test_caught_raises_self_and_branch():
    """被接住：self 层 +gain、branch 层 +0.5×gain（聊得起来，更想聊）。"""
    engine = _make_engine(urge_base=0.7, urge_gain=0.1)
    state = engine.load_self_state()
    state["state"]["urge"] = 0.7
    engine.save_self_state(state)
    branch = engine.load_branch_state(_UID)
    branch["state"]["urge_factor"] = 0.8
    engine.save_branch_state(_UID, branch)

    engine.record_urge_feedback(_UID, "caught")

    self_urge = float(engine.load_self_state()["state"]["urge"])
    factor = float(engine.load_branch_state(_UID)["state"]["urge_factor"])
    assert self_urge == 0.8, f"self 层应 +0.1 → 0.8（实际 {self_urge}）"
    assert factor == 0.85, f"branch 层应 +0.05 → 0.85（实际 {factor}）"


def test_ignored_lowers_with_branch_floor():
    """被冷落：self/branch 双降；branch 不低于 urge_branch_floor（防彻底饿死）。"""
    engine = _make_engine(urge_base=0.7, urge_decay=0.25, urge_branch_floor=0.4)
    state = engine.load_self_state()
    state["state"]["urge"] = 0.5
    engine.save_self_state(state)
    branch = engine.load_branch_state(_UID)
    branch["state"]["urge_factor"] = 0.5
    engine.save_branch_state(_UID, branch)

    engine.record_urge_feedback(_UID, "ignored")
    engine.record_urge_feedback(_UID, "ignored")  # 连续冷落两次 → branch 触底

    self_urge = float(engine.load_self_state()["state"]["urge"])
    factor = float(engine.load_branch_state(_UID)["state"]["urge_factor"])
    assert self_urge == 0.05, f"self 层应 0.5-0.25×2 → 触底 clamp 0.05（实际 {self_urge}）"
    assert factor == 0.4, f"branch 层触底后应为 floor 0.4（实际 {factor}）"


def test_user_initiated_only_self():
    """用户主动发起：仅 self 层小升（+0.5×gain），branch 层不动。"""
    engine = _make_engine(urge_base=0.7, urge_gain=0.1)
    state = engine.load_self_state()
    state["state"]["urge"] = 0.7
    engine.save_self_state(state)
    branch = engine.load_branch_state(_UID)
    branch["state"]["urge_factor"] = 0.9
    engine.save_branch_state(_UID, branch)

    engine.record_urge_feedback(_UID, "user_initiated")

    self_urge = float(engine.load_self_state()["state"]["urge"])
    factor = float(engine.load_branch_state(_UID)["state"]["urge_factor"])
    assert self_urge == 0.75, f"self 层应 +0.05 → 0.75（实际 {self_urge}）"
    assert factor == 0.9, "branch 层不应被 user_initiated 改动"


def test_unknown_event_raises():
    """未知事件立即抛错（不兜底掩盖调用方笔误）。"""
    engine = _make_engine()
    try:
        engine.record_urge_feedback(_UID, "whatever")
    except ValueError as exc:
        assert "whatever" in str(exc)
    else:
        raise AssertionError("未知事件类型未抛 ValueError")


def test_feedback_gate_switches_off():
    """plugin / narrative / urge 任一开关关闭 → 状态零改动。"""
    for kwargs in (
        {"plugin_enabled": False},
        {"narrative_enabled": False},
        {"urge_enabled": False},
    ):
        engine = _make_engine(**kwargs)
        state = engine.load_self_state()
        state["state"]["urge"] = 0.5
        engine.save_self_state(state)

        engine.record_urge_feedback(_UID, "ignored")

        assert float(engine.load_self_state()["state"]["urge"]) == 0.5, (
            f"开关关闭时反馈不应生效（{kwargs}）"
        )


# ===== engine 层：合成 =====


def test_compute_multiply_formula():
    """合成 = self × branch × 精力因子（精力=基线 → 因子 1.0）。"""
    engine = _make_engine(urge_base=0.7, energy_baseline=0.45)
    state = engine.load_self_state()
    state["state"]["urge"] = 0.5
    engine.save_self_state(state)
    branch = engine.load_branch_state(_UID)
    branch["state"]["urge_factor"] = 0.8
    engine.save_branch_state(_UID, branch)

    urge = engine.compute_share_urge(_UID)
    assert urge == 0.4, f"0.5 × 0.8 × 1.0 应为 0.4（实际 {urge}）"


def test_compute_low_energy_drag():
    """精力低于基线 → 按比例拖累，因子下限 0.4（累了不想说话）。"""
    engine = _make_engine(urge_base=0.7, energy_baseline=0.45)
    state = engine.load_self_state()
    state["state"]["urge"] = 0.8
    state["state"]["mood"]["energy"] = 0.18  # 0.18/0.45 = 0.4 → 因子恰好下限
    engine.save_self_state(state)
    engine.load_branch_state(_UID)  # branch 缺省中性 1.0

    urge = engine.compute_share_urge(_UID)
    assert urge == 0.32, f"0.8 × 1.0 × 0.4 应为 0.32（实际 {urge}）"


def test_compute_gate_returns_full():
    """开关任一关闭 → 返回 1.0（旧行为：到点必发）。"""
    for kwargs in (
        {"plugin_enabled": False},
        {"narrative_enabled": False},
        {"urge_enabled": False},
    ):
        engine = _make_engine(**kwargs)
        assert engine.compute_share_urge(_UID) == 1.0, f"gate 关闭应返回 1.0（{kwargs}）"


# ===== scheduler 层：冷落结算与采样接线 =====


def test_settle_expired_counts_ignored():
    """settle_expired：超窗且已送达 → 计冷落；窗口内记录保留。"""
    scheduler, _, _ = _make_scheduler()
    fresh = _NOW - datetime.timedelta(hours=1)
    expired = _NOW - datetime.timedelta(hours=17)
    # 先登记超窗那条并确认送达，再登记窗口内那条（mark_delivered 打在最新一条上）
    scheduler.record_sent(_UID, f"stream-{_UID}", expired, "甲")
    scheduler.mark_delivered(f"stream-{_UID}")
    scheduler.record_sent(_UID, f"stream-{_UID}", fresh, "乙")

    ignored, undelivered = scheduler.settle_expired(_UID, _NOW)

    assert (ignored, undelivered) == (1, 0), f"应计 1 次冷落（实际 {ignored}, {undelivered}）"
    assert len(scheduler._sent_records[_UID]) == 1, "窗口内记录应保留"


def test_check_once_settles_ignored_feedback():
    """调度循环：超窗已送达条目 → 引擎收到 ignored 反馈（出队即罚，不重复）。"""
    scheduler, _, urge_events = _make_scheduler()
    scheduler.record_sent(_UID, f"stream-{_UID}", _NOW - datetime.timedelta(hours=20), "甲")
    scheduler.mark_delivered(f"stream-{_UID}")

    asyncio.run(scheduler._check_once())
    asyncio.run(scheduler._check_once())  # 第二轮不得重复惩罚

    assert urge_events == [(_UID, "ignored")], (
        f"过期条目应恰好结算一次冷落（实际 {urge_events}）"
    )


def test_check_once_undelivered_is_not_punished():
    """未送达的超窗条目 → 记 undelivered，**不罚冷落**（用户没看到，不该罚）。"""
    scheduler, _, urge_events = _make_scheduler()
    scheduler.record_sent(_UID, f"stream-{_UID}", _NOW - datetime.timedelta(hours=20), "甲")
    # 故意不 mark_delivered：模拟模型选择沉默 / reply 工具失败

    asyncio.run(scheduler._check_once())

    assert urge_events == [], f"未送达不应产生冷落惩罚（实际 {urge_events}）"
    assert scheduler._metrics == [("proactive_undelivered", 1, _UID, "proactive")], (
        f"应记一条 undelivered（实际 {scheduler._metrics}）"
    )


def test_check_once_full_urge_fires():
    """分享欲恒满（1.0）：到点必开口（旧行为不回退）。"""
    scheduler, fired, _ = _make_scheduler(urge=1.0)
    scheduler._next_fire[_UID] = _NOW - datetime.timedelta(minutes=1)  # 已到点

    asyncio.run(scheduler._check_once())

    assert fired == [_UID], f"到点且分享欲满 → 应触发主动开口（实际 {fired}）"


def test_check_once_zero_urge_reschedules():
    """分享欲为零：不开口，且 30-60 分钟后重试（不重置完整随机间隔）。"""
    scheduler, fired, _ = _make_scheduler(urge=0.0)
    scheduler._next_fire[_UID] = _NOW - datetime.timedelta(minutes=1)

    asyncio.run(scheduler._check_once())

    assert fired == [], "分享欲为 0 时不得开口"
    next_at = scheduler._next_fire[_UID]
    delay = (next_at - _NOW).total_seconds() / 60
    assert 30 <= delay <= 60, f"重试间隔应落在 30-60 分钟（实际 {delay:.0f}）"


def test_check_once_urge_disabled_keeps_old_behavior():
    """总开关关闭：compute 返回 1.0 → 到点必发（与改造前行为一致）。"""
    scheduler, fired, _ = _make_scheduler(urge=0.0, urge_enabled=False)
    scheduler._next_fire[_UID] = _NOW - datetime.timedelta(minutes=1)

    asyncio.run(scheduler._check_once())

    assert fired == [_UID], "分享欲开关关闭时应保持旧行为（到点必发）"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
