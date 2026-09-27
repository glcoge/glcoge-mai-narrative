"""晋升 tick 的写入时刻语义（2026-09-27 修复锁）。

背景：``_maybe_run_promotion`` 原先在函数入口取一次 ``now``，一路传到
``apply`` / ``apply_refutations`` / ``promote_relationship``。但 ``run()`` 里
**含 LLM 调用**（实测约 25 秒），导致事后写入的审计时间戳记的是「周期开始」
而非「实际写入」—— ``promotions.ts`` 系统性偏早，无法与计数器、日志时间线对账。

修复：``run`` 之后另取一次新鲜时刻（``write_now``）专供写入使用。
本文件把这条语义钉住，避免某次「顺手精简」把两个时刻又合并回去。

运行（项目根）::

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_promotion_tick_ts.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import types
from typing import Any, Dict, List, Tuple

import _synth_loader

# 真正执行 services/__init__.py（plugin.py 依赖其再导出），子模块走标准导入机制
_synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin

#: 一次提案提炼的 LLM 耗时量级；用它把「提炼前 / 后」拉开距离便于断言
_LLM_COST = datetime.timedelta(seconds=25)


class _Clock:
    """每次调用前进一个 LLM 提炼的耗时，模拟 ``run()`` 前后的真实时间流逝。"""

    def __init__(self, base: datetime.datetime) -> None:
        self._base = base
        self.calls: List[datetime.datetime] = []

    def __call__(self) -> datetime.datetime:
        moment = self._base + _LLM_COST * len(self.calls)
        self.calls.append(moment)
        return moment


def _config(enabled: bool = True) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        plugin=types.SimpleNamespace(enabled=enabled),
        narrative=types.SimpleNamespace(enabled=enabled),
        promotion=types.SimpleNamespace(enabled=enabled),
    )


class _Runner:
    """假提案提炼器：记录收到的时刻，返回可 await 的结果。"""

    def __init__(self, status: str) -> None:
        self.status = status
        self.seen: datetime.datetime | None = None

    async def run(self, *, now: datetime.datetime) -> Dict[str, Any]:
        self.seen = now
        return {"status": self.status}


class _Engine:
    """假晋升机：只记录收到的 ``now``，不做任何真实判定。"""

    def __init__(self) -> None:
        self.refutations_now: datetime.datetime | None = None
        self.applied_now: datetime.datetime | None = None

    def apply_refutations(self, *, now: datetime.datetime) -> List[int]:
        self.refutations_now = now
        return []

    def apply(self, proposal: Any, *, now: datetime.datetime) -> Dict[str, Any]:
        self.applied_now = now
        return {"applied": False}


def _build(status: str) -> Tuple[Any, _Clock, _Runner, _Engine, List, datetime.datetime]:
    base = datetime.datetime(2026, 9, 27, 15, 0, 0)
    clock = _Clock(base)
    runner = _Runner(status)
    engine = _Engine()
    relation_calls: List[datetime.datetime] = []
    plugin = types.SimpleNamespace(
        config=_config(),
        _promotion=runner,
        _promotion_engine=engine,
        _store=types.SimpleNamespace(list_proposals=lambda **_kw: []),
        _local_now=clock,
        _maybe_promote_relationship=lambda now: relation_calls.append(now),
    )
    return plugin, clock, runner, engine, relation_calls, base


def test_write_timestamp_is_fresher_than_distillation_moment():
    """核心断言：写入时刻必须**晚于**提案提炼的判定时刻。"""
    plugin, _clock, runner, engine, relation_calls, base = _build("ok")

    asyncio.run(MaiNarrativePlugin._maybe_run_promotion(plugin))

    assert runner.seen == base, "提案提炼应沿用入口时刻（那是节流判定语义）"
    assert engine.refutations_now is not None, "提炼成功时应执行反证扫描"
    assert engine.refutations_now > runner.seen, (
        "反证写入必须晚于提炼时刻：沿用旧 now 会把 promotions.ts 记成周期开始"
    )
    assert engine.refutations_now == base + _LLM_COST
    assert relation_calls == [base + _LLM_COST], "关系晋升同样要用新鲜时刻"


def test_relationship_gets_fresh_moment_even_when_not_due():
    """run 不到时机时，关系晋升仍应拿到新鲜时刻（节流用当前时刻才准）。"""
    plugin, _clock, runner, engine, relation_calls, base = _build("skipped")

    asyncio.run(MaiNarrativePlugin._maybe_run_promotion(plugin))

    assert runner.seen == base
    assert engine.refutations_now is None, "不到提炼时机时不该执行反证/晋升"
    assert relation_calls == [base + _LLM_COST]


def test_clock_is_read_exactly_twice_per_tick():
    """一次 tick 恰好评两次时钟：提炼判定一次 + 写入一次，不多不少。"""
    plugin, clock, _runner, _engine, _relation_calls, base = _build("ok")

    asyncio.run(MaiNarrativePlugin._maybe_run_promotion(plugin))

    assert clock.calls == [base, base + _LLM_COST], (
        "多取或少取都会让时刻语义重新变得含糊"
    )


def test_promotion_disabled_short_circuits_without_touching_clock():
    """晋升开关关闭时直接短路，连时钟都不该读。"""
    plugin, clock, runner, engine, relation_calls, base = _build("ok")
    plugin.config.promotion = types.SimpleNamespace(enabled=False)

    asyncio.run(MaiNarrativePlugin._maybe_run_promotion(plugin))

    assert clock.calls == []
    assert runner.seen is None
    assert engine.refutations_now is None
    assert relation_calls == []
