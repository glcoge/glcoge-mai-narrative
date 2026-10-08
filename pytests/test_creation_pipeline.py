"""创作管道基元测试（深化 A / 架构体检候选 A）。

pipeline.py 只承载两管道的**真重复**（闸门基元 + 守卫拒绝日志 + 落账尾段）；
闸门顺序与间隔键推进时机是真语义差异，编排保留在 life/seeder 本地（见
`.workbuddy/v0.3重构/A+D执行方案.md` §0.1 差异表）——本文件锁基元自身契约，
两管道的行为面由既有测试锁（test_fragment_pending_capacity / test_seeder /
test_life_fragment_highlight / test_kvkeys）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_creation_pipeline.py -q
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import _synth_loader
from pytests._synth_loader import load  # noqa: E402

_PIPELINE = load("services.creation.pipeline")

interval_gate = _PIPELINE.interval_gate
daily_cap_gate = _PIPELINE.daily_cap_gate
log_guard_reject = _PIPELINE.log_guard_reject
commit_created_event = _PIPELINE.commit_created_event

_NOW = datetime.datetime(2026, 10, 8, 12, 0, 0)


class _Logger:
    def __init__(self):
        self.warnings: list = []

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        self.warnings.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))

    def error(self, *a, **k):
        pass


class _FakeStore:
    """闸门/落账基元所需的最小 store（kv + chronicle）。"""

    def __init__(self):
        self.kv_str: dict = {}
        self.kv_int: dict = {}
        self.chronicle: list = []

    def get_kv_str(self, key, default=""):
        return self.kv_str.get(key, default)

    def set_kv_str(self, key, value):
        self.kv_str[key] = value

    def get_kv_int(self, key, default=0):
        return self.kv_int.get(key, default)

    def set_kv_int(self, key, value):
        self.kv_int[key] = value

    def append_chronicle(self, scope, kind, text, ts=None):
        self.chronicle.append({"scope": scope, "kind": kind, "text": text, "ts": ts})


def _make_engine(*, chronicle_enabled=True, pending_max=12):
    config = SimpleNamespace(
        narrative=SimpleNamespace(
            chronicle_enabled=chronicle_enabled,
            fragment_pending_max=pending_max,
        )
    )
    store = _FakeStore()
    saved: list = []
    engine = SimpleNamespace(
        _plugin=SimpleNamespace(config=config),
        _store=store,
        save_self_state=lambda state: saved.append(state),
    )
    return engine, store, saved


# ===== 闸门基元 =====


def test_interval_gate_no_record_passes():
    """无间隔记录 → 放行（首次生成不受闸）。"""
    store = _FakeStore()
    assert interval_gate(store, "life_fragment:last_ts", _NOW, minutes=120) is True


def test_interval_gate_blocks_within_window():
    """窗内 → 拦截。"""
    store = _FakeStore()
    store.set_kv_str("k", (_NOW - datetime.timedelta(minutes=30)).isoformat(timespec="seconds"))
    assert interval_gate(store, "k", _NOW, minutes=120) is False


def test_interval_gate_boundary_exact_minutes_passes():
    """恰好等于 minutes → 放行（现行语义：total_seconds() < minutes*60 才拦）。"""
    store = _FakeStore()
    store.set_kv_str("k", (_NOW - datetime.timedelta(minutes=120)).isoformat(timespec="seconds"))
    assert interval_gate(store, "k", _NOW, minutes=120) is True


def test_interval_gate_malformed_ts_passes():
    """坏时间戳 → 放行（life/seeder 同语义：解析不出视为无闸）。"""
    store = _FakeStore()
    store.set_kv_str("k", "不是时间")
    assert interval_gate(store, "k", _NOW, minutes=120) is True


def test_daily_cap_gate():
    """日上限：未达放行、恰达拦截、cap=0 防御为 1（与 seeder 现行 max(1,·) 同）。"""
    store = _FakeStore()
    assert daily_cap_gate(store, "k", cap=2) is True
    store.set_kv_int("k", 1)
    assert daily_cap_gate(store, "k", cap=2) is True
    store.set_kv_int("k", 2)
    assert daily_cap_gate(store, "k", cap=2) is False
    store.set_kv_int("k", 1)
    assert daily_cap_gate(store, "k", cap=0) is False, "cap=0 防御为 1：1 >= 1 拦截"


# ===== 守卫拒绝日志（文案逐字节兼容两管道现行格式） =====


def test_log_guard_reject_with_detail():
    """life 形状：label + detail + 原文（>60 字截断加省略号）。"""
    logger = _Logger()
    log_guard_reject(
        logger,
        label="生活片段命中锚定守卫（world_rules/values/禁用片段）",
        detail="命中片段: ['尾巴']",
        text="甲" * 80,
    )
    assert len(logger.warnings) == 1
    message = logger.warnings[0]
    assert message == (
        "生活片段命中锚定守卫（world_rules/values/禁用片段）→ 已丢弃，不入库；"
        "命中片段: ['尾巴']；原文: " + "甲" * 60 + "…"
    )


def test_log_guard_reject_without_detail_no_ellipsis():
    """seeder 形状：无 detail 段；短原文不截断不加省略号。"""
    logger = _Logger()
    log_guard_reject(
        logger,
        label="播种事件命中参与者拦截（命中=10001）",
        detail="",
        text="短原文",
        ellipsis=False,
    )
    assert logger.warnings[0] == "播种事件命中参与者拦截（命中=10001）→ 已丢弃，不入库；原文: 短原文"


# ===== 落账尾段（LRU + save + chronicle 条件写） =====


def test_commit_appends_and_saves():
    """entry 进 pending_events + save 被调 + chronicle 写入（kind/ts 正确）。"""
    engine, store, saved = _make_engine()
    state = {"state": {"focus": {"pending_events": []}}}
    entry = {"ts": _NOW.isoformat(timespec="seconds"), "text": "一段新生活", "highlight": False}

    commit_created_event(
        engine, state, entry=entry, chronicle_kind="life", now=_NOW
    )

    assert state["state"]["focus"]["pending_events"] == [entry]
    assert len(saved) == 1
    assert store.chronicle == [
        {"scope": "self", "kind": "life", "text": "一段新生活", "ts": _NOW.isoformat(timespec="seconds")}
    ]


def test_commit_lru_truncates():
    """容量 2 塞 3 条 → 最早一条被挤出（与既有 test_fragment_pending_capacity 同语义）。"""
    engine, store, saved = _make_engine(pending_max=2)
    state = {"state": {"focus": {"pending_events": [{"ts": "t1", "text": "最旧"}]}}}

    commit_created_event(
        engine, state, entry={"ts": "t2", "text": "次新"}, chronicle_kind="life", now=_NOW
    )
    commit_created_event(
        engine, state, entry={"ts": "t3", "text": "最新"}, chronicle_kind="life", now=_NOW
    )

    pending = state["state"]["focus"]["pending_events"]
    assert [item["text"] for item in pending] == ["次新", "最新"]


def test_commit_chronicle_disabled_skips_write():
    """chronicle_enabled=False → 只进 pending，不写编年史（pending 不断供，2026-09-16 语义）。"""
    engine, store, saved = _make_engine(chronicle_enabled=False)
    state = {"state": {"focus": {"pending_events": []}}}

    commit_created_event(
        engine, state, entry={"ts": "t", "text": "片段"}, chronicle_kind="life_seed", now=_NOW
    )

    assert state["state"]["focus"]["pending_events"]
    assert store.chronicle == []


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
