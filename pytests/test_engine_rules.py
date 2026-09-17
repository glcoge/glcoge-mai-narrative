"""narrative 世界引擎纯规则测试（energy 回归测试，T2④）。

narrative 插件目录名含连字符（``glcoge-mai-narrative``），不能直接作为包名
import——借鉴 mai-diary 的做法，用 ``importlib`` 挂到合成包名
``_narrative_test_plugin`` 下加载。

背景（2026-09-08 周回顾定案）：老规则每 tick 无条件 -0.06、深夜反而 -0.08、
唯一回升途径是 2h 内互动，导致无互动日 mood 必然贴地 0.05 卡死"疲惫"。
新规则：向基线回归（双向）+ 互动提振 + 深夜睡眠恢复，三参数入 config。

运行（项目根）：

    # 1) pytest
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_engine_rules.py -q

    # 2) 独立脚本
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_engine_rules.py
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_ENGINE = _synth_loader.load("services.engine")

NarrativeEngine = _ENGINE.NarrativeEngine
mood_by_energy = _ENGINE.mood_by_energy


# ===== 构造工具 =====


def _make_engine(
    *,
    baseline: float = 0.45,
    pull: float = 0.3,
    sleep_recovery: float = 0.1,
    boost: float = 0.12,
    urge_enabled: bool = True,
    urge_base: float = 0.7,
    urge_regain: float = 0.15,
):
    """构造只含纯规则所需依赖的 engine（绕过 __init__，不建 store/creator）。

    v0.1.8 起补 proactive 组：_apply_state_rules 里的 share_urge 基线回归
    需要 urge_* 参数（默认 urge_enabled=True 时回归生效）。
    """
    cfg = SimpleNamespace(
        narrative=SimpleNamespace(
            enabled=True,
            clock_tick_minutes=30,
            energy_baseline=baseline,
            energy_baseline_pull=pull,
            energy_sleep_recovery=sleep_recovery,
            energy_interaction_boost=boost,
        ),
        proactive=SimpleNamespace(
            urge_enabled=urge_enabled,
            urge_base=urge_base,
            urge_regain=urge_regain,
            urge_gain=0.1,
            urge_decay=0.25,
            urge_branch_floor=0.4,
        ),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(config=cfg)
    return engine


def _make_state(energy: float, last_interaction: str = "") -> dict:
    """构造 _apply_state_rules 所需的最小 state 结构。"""
    return {
        "state": {
            "mood": {
                "label": mood_by_energy(energy),
                "energy": energy,
                "last_shift_ts": "",
            },
            "routine": {"phase": "清晨", "sleep_time": "23:30", "wake_time": "07:00"},
            "focus": {"hot_thread": "", "pending_events": []},
            "habits": [],
            "last_interaction_ts": last_interaction,
            "last_talk_date": "",
        }
    }


def _simulate(
    engine: NarrativeEngine,
    state: dict,
    *,
    start_hour: int,
    ticks: int,
    interaction: bool = False,
    tick_minutes: int = 30,
) -> list:
    """从当日 start_hour 起逐 tick 推进 _apply_state_rules，返回每次 tick 后的 energy。

    interaction=True 表示每个 tick 前 5 分钟都有用户互动（2h 窗口内）。
    """
    now = datetime.datetime(2026, 9, 9, start_hour, 0, 0)
    energies: list = []
    for _ in range(ticks):
        if interaction:
            state["state"]["last_interaction_ts"] = (
                now - datetime.timedelta(minutes=5)
            ).isoformat(timespec="seconds")
        engine._apply_state_rules(state, now)
        energies.append(float(state["state"]["mood"]["energy"]))
        now += datetime.timedelta(minutes=tick_minutes)
    return energies


# ===== 回归用例 =====


def test_no_interaction_day_recovers():
    """回归核心：无互动日白天（06:00 起 17h）精力应回摆到"平静"档，不再贴地。"""
    engine = _make_engine()
    state = _make_state(0.05)
    energies = _simulate(engine, state, start_hour=6, ticks=34)

    assert energies[-1] >= 0.40, (
        f"17h 无互动后精力 {energies[-1]:.2f}，未回摆到平静档（≥0.40）"
    )
    assert state["state"]["mood"]["label"] == "平静"


def test_night_sleep_restores():
    """睡眠回血：整夜（23:00 起 12 tick 全深夜）无互动，精力应达"轻快"档（≥0.70）。"""
    engine = _make_engine()
    state = _make_state(0.45)
    night = _simulate(engine, state, start_hour=23, ticks=12)

    assert night[-1] >= 0.70, (
        f"整夜睡眠后精力 {night[-1]:.2f}，未达轻快档（≥0.70）"
    )

    # 清晨醒来后无互动：基线回归应让精力自然回落（不平复反而说明回归项坏了）
    morning = _simulate(engine, state, start_hour=5, ticks=6)
    assert morning[-1] < night[-1], (
        f"清晨无互动精力 {morning[-1]:.2f} 应低于睡眠峰值 {night[-1]:.2f}"
    )


def test_active_interaction_maintains():
    """白天持续互动：精力应维持高位（≥0.60），不因基线回归被拉低。"""
    engine = _make_engine()
    state = _make_state(0.60)
    energies = _simulate(engine, state, start_hour=10, ticks=16, interaction=True)

    assert energies[-1] >= 0.60, (
        f"8h 持续互动后精力 {energies[-1]:.2f}，应维持 ≥0.60"
    )


# ===== share_urge self 层基线回归（v0.1.8 第一步） =====


def test_urge_regresses_to_baseline():
    """被冷落砸低的分享欲，随 tick 向基线双向回归（同 energy 模式）。"""
    engine = _make_engine(urge_base=0.7, urge_regain=0.15)
    state = _make_state(0.60)
    state["state"]["urge"] = 0.3  # 模拟刚被冷落过的低谷

    now = datetime.datetime(2026, 9, 9, 10, 0, 0)
    for _ in range(10):
        engine._apply_state_rules(state, now)
        now += datetime.timedelta(minutes=30)

    urge = float(state["state"]["urge"])
    assert urge > 0.45, f"10 tick 后分享欲 {urge} 应显著回温（>0.45）"
    assert urge < 0.7, f"尚未到基线前不应越过基线（实际 {urge}）"


def test_urge_regression_disabled_when_switch_off():
    """urge_enabled=False：tick 不写 urge 字段（完全旧行为，状态零污染）。"""
    engine = _make_engine(urge_enabled=False)
    state = _make_state(0.60)
    state["state"]["urge"] = 0.3

    engine._apply_state_rules(state, datetime.datetime(2026, 9, 9, 10, 0, 0))

    assert state["state"]["urge"] == 0.3, "开关关闭时 tick 不得改动分享欲"


def test_urge_missing_field_defaults_to_base():
    """旧数据无 urge 字段：首个 tick 以基线为起点惰性初始化。"""
    engine = _make_engine(urge_base=0.7, urge_regain=0.15)
    state = _make_state(0.60)
    assert "urge" not in state["state"]

    engine._apply_state_rules(state, datetime.datetime(2026, 9, 9, 10, 0, 0))

    assert state["state"]["urge"] == 0.7, "缺字段时应以 urge_base 惰性初始化"


def test_energy_clamped():
    """边界：任意场景下 energy 都不越 [0.05, 1.0]。"""
    engine = _make_engine()
    for start_e, hour, ticks in [(1.0, 12, 48), (0.05, 0, 48)]:
        state = _make_state(start_e)
        energies = _simulate(engine, state, start_hour=hour, ticks=ticks)
        assert all(0.05 <= e <= 1.0 for e in energies), (
            f"start={start_e} hour={hour} 出现越界值: {min(energies):.3f}~{max(energies):.3f}"
        )


def test_config_params_take_effect():
    """参数化：更大的回归系数应产生更快的恢复速度（A/B 可调的前提）。"""
    fast = _make_engine(pull=0.6)
    slow = _make_engine(pull=0.05)
    e_fast = _simulate(fast, _make_state(0.05), start_hour=10, ticks=6)
    e_slow = _simulate(slow, _make_state(0.05), start_hour=10, ticks=6)

    assert e_fast[-1] > e_slow[-1], (
        f"pull=0.6 终值 {e_fast[-1]:.3f} 应高于 pull=0.05 终值 {e_slow[-1]:.3f}"
    )


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
