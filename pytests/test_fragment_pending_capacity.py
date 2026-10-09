"""素材池容量（pending_events 有界保留）的落库行为（方案 §7 / P20，2026-09-30）。

背景：容量此前硬编码 5，是在「日上限 3~6」时代定的；日上限放宽到 16 后 5 条
显得过窄，主动消息可选素材被掐断。本批改为配置项 ``fragment_pending_max``
（默认 12），由头端同时改为**全窗口取材**。

分工（两个语义别混）：
- **容量**＝生活片段落库时的有界保留，LRU 淘汰最早一条 → 本文件；
- **取材窗口**＝由头签发时读多少条候选 → ``test_sourcing.py``。

本文件只锁创作侧：连续生成超过容量时，最早入库的一条被挤出，最新一条保留。
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import KvStoreMixin, load, make_life_engine, pending_events as _pending  # noqa: E402

_NarrativeEngine = load("services.state.engine").NarrativeEngine
life = load("services.creation.life")

_BASE = datetime.datetime(2026, 9, 21, 8, 0, 0)


class _FakeStore(KvStoreMixin):
    """只含生活片段落库链路所需接口的假 store。"""

    def __init__(self):
        self.chronicle: list = []
        self.kv_int: dict = {}
        self.kv_str: dict = {}

    def get_kv_int(self, key):
        return self.kv_int.get(key, 0)

    def set_kv_int(self, key, value):
        self.kv_int[key] = value

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value

    def list_events(self, scope, limit=20):
        return []

    def append_chronicle(self, scope, kind, text, ts):
        self.chronicle.append({"scope": scope, "kind": kind, "text": text, "ts": ts})


def _make_engine(*, interval=30, daily_max=16, pending_max=12):
    """构造只含生活片段链路依赖的 engine（装配收敛至 _synth_loader.make_life_engine）。"""
    return make_life_engine(interval=interval, daily_max=daily_max, pending_max=pending_max)


def _generate(engine, count, *, step_minutes=31):
    """按固定步长生成 count 段生活片段（每步跨过 interval 闸门）。"""
    for index in range(count):
        moment = _BASE + datetime.timedelta(minutes=step_minutes * index)
        asyncio.run(life.maybe_generate_life_fragment(engine.deps,moment))


def test_capacity_lru_evicts_oldest():
    """容量 12（配置默认）：生成 13 段后恒留 12 条，最早一条被挤出。"""
    engine = _make_engine(pending_max=12)
    _generate(engine, 13)

    pending = _pending(engine)
    assert len(pending) == 12, "容量应恒为 12"
    # 第 1 次入库的条目被挤出；留下的最早一条是第 2 次
    assert pending[0]["ts"] == (_BASE + datetime.timedelta(minutes=31)).isoformat(
        timespec="seconds"
    )
    assert pending[-1]["ts"] == (_BASE + datetime.timedelta(minutes=31 * 12)).isoformat(
        timespec="seconds"
    )


def test_capacity_respects_small_config():
    """容量跟随配置：pending_max=3 时 5 段后只剩 3 条。"""
    engine = _make_engine(pending_max=3)
    _generate(engine, 5)

    pending = _pending(engine)
    assert len(pending) == 3
    assert pending[0]["ts"] == (_BASE + datetime.timedelta(minutes=31 * 2)).isoformat(
        timespec="seconds"
    )


def test_capacity_zero_treated_as_one():
    """容量 0 防御：pending[-0:] 是「全量」不是「空」，代码须按 1 处理。"""
    engine = _make_engine(pending_max=0)
    _generate(engine, 3)

    assert len(_pending(engine)) == 1


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
