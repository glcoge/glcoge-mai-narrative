"""回放台最小夹具：构造 `build_context_block` 所需的 plugin / state / branch。

与 pytests 的 `_synth_loader` 分开放：回放台要能被 CLI 独立运行，
不依赖 pytest 目录结构。SDK 隔离靠 `_synth_loader.load` 完成（见 `runtime.py`）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List

# 锚定层（identity）：回放只需非空占位，内容不影响泄露断言
IDENTITY = {
    "world": "回放台占位世界观",
    "values": ["回放台占位价值观"],
    "world_rules": ["回放台占位规则"],
}


def plugin_stub(*, sleep_enabled: bool = False) -> Any:
    """最小 plugin 假件。

    Args:
        sleep_enabled: 是否启用睡眠态。默认关闭（`sleep_time=""`）——
            回放用例只关心隔离与否，不需要睡眠提示介入。
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        config=SimpleNamespace(
            identity=SimpleNamespace(**IDENTITY),
            narrative=SimpleNamespace(
                sleep_time="23:30" if sleep_enabled else "",
                wake_time="07:00" if sleep_enabled else "",
                sleep_delay_max_minutes=60,
                sleep_delay_recent_minutes=10,
                woken_awake_minutes=30,
                energy_woken_penalty=0.08,
                energy_woken_floor=0.3,
                wake_fragment_enabled=sleep_enabled,
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


def self_state(
    *,
    pending_events: List[Dict[str, Any]] | None = None,
    last_interaction_ts: str = "",
) -> Dict[str, Any]:
    """自我层状态骨架（字段口径对齐 `state/engine.py` 的 `default_self_state`）。"""
    return {
        "identity": {},
        "state": {
            "mood": {"label": "平静", "energy": 0.6, "last_shift_ts": ""},
            "routine": {"phase": "下午", "sleep_time": "", "wake_time": ""},
            "schedule": [],
            "habits": [],
            "focus": {"hot_thread": "", "pending_events": list(pending_events or [])},
            "last_interaction_ts": last_interaction_ts,
        },
        "chronicle": {"entries": []},
    }


def branch_state(*, stage: str = "熟人", first_met: str = "2026-09-01T10:00:00") -> Dict[str, Any]:
    """支线层状态骨架。"""
    return {
        "identity": {"stage": stage, "first_met": first_met, "shared_secrets": []},
        "state": {
            "trust": 10.0,
            "familiarity": 10.0,
            "last_interaction_ts": "",
            "user_notes": {},
            "milestones": [],
        },
        "chronicle": {"entries": []},
    }


def now() -> _dt.datetime:
    """固定回放时刻，保证用例可重复。"""
    return _dt.datetime(2026, 9, 22, 11, 37)
