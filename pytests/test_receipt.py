"""兑现回执（v0.3.0 批 0 / 执行路线 §批 0-4）。

链路：签发（``record_sent`` 带 event_id/origin）→ 送达确认（``mark_delivered``）
→ 承接结算（``resolve_catch`` 命中）→ ``telemetry.record("receipt_catch", 延迟分钟,
user_id, scope=origin)``。

口径裁定（批 0 执行方案 §3）：
- **只记指标，不做一致性约束**——由头被岔开 = 特性，不写回状态、不罚不发；
- 走通用 ``record`` 通道（scope=origin），不走晋升专属的 ``record_counter``；
- 未送达 / 超窗 / 已结算 / telemetry 关闭 / 无 telemetry 一律不落（门控继承既有语义）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_receipt.py -q
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader
from pytests._synth_loader import FakeLogger, FakeStore, make_logger  # noqa: E402

_SCHEDULER = _synth_loader.load("services.proactive.scheduler")
_SNAPSHOT = _synth_loader.load("services.state.snapshot")

ProactiveScheduler = _SCHEDULER.ProactiveScheduler
Telemetry = _SNAPSHOT.Telemetry

_NOW = datetime.datetime(2026, 10, 7, 12, 0, 0)
_UID = "10001"






def _make_scheduler(*, telemetry_enabled=True, with_telemetry=True):
    """scheduler + telemetry + 捕获 store 的完整装配。"""
    store = FakeStore()
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        telemetry=SimpleNamespace(enabled=telemetry_enabled),
    )
    plugin = SimpleNamespace(
        config=config,
        ctx=SimpleNamespace(logger=make_logger()),
        _store=store,
        # 深化 C3b：scheduler 的 store 读经 engine.deps（与生产同构）
        _engine=SimpleNamespace(deps=SimpleNamespace(store=store)),
    )
    if with_telemetry:
        # 与生产同构：Telemetry 挂 plugin._telemetry（scheduler 经 plugin 取用）
        plugin._telemetry = Telemetry(plugin)
    sched = ProactiveScheduler.__new__(ProactiveScheduler)
    sched._plugin = plugin
    sched._task = None
    sched._running = False
    sched._next_fire = {}
    sched._sent_records = {}
    sched._pending_at = {}
    sched._engaged_windows = {}
    return sched, store


def _receipts(store):
    return [m for m in store.metrics if m["name"] == "receipt_catch"]


def _sign_delivered(sched, uid=_UID, stream="stream-1", **kwargs):
    """登记一次已确认送达的主动开口（承接判定的前置条件）。"""
    sched.record_sent(uid, stream, _NOW, "最近一段生活：煮了粥", **kwargs)
    sched.mark_delivered(stream)


# ===== 正链路 =====


def test_receipt_caught_records_latency_and_scope():
    """签发→送达→承接：落 receipt_catch，value=延迟分钟、scope=origin。"""
    sched, store = _make_scheduler()
    _sign_delivered(sched, event_id="ev_abc", origin="fragment")

    latency = sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=5))
    assert latency is not None

    receipts = _receipts(store)
    assert len(receipts) == 1
    assert abs(receipts[0]["value"] - 5.0) < 1e-6
    assert receipts[0]["scope"] == "fragment"
    assert receipts[0]["user_id"] == _UID


def test_receipt_scope_mood():
    """mood 由头（无事件实体）：scope=mood、照常落回执——scope 是来源分析维度。"""
    sched, store = _make_scheduler()
    _sign_delivered(sched, event_id="", origin="mood")

    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=2)) is not None
    receipts = _receipts(store)
    assert len(receipts) == 1
    assert receipts[0]["scope"] == "mood"


def test_receipt_scope_milestone():
    """milestone 由头：event_id=milestone_id 透传、scope=milestone。"""
    sched, store = _make_scheduler()
    _sign_delivered(sched, event_id="engaged:20261007", origin="milestone")

    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=1)) is not None
    assert _receipts(store)[0]["scope"] == "milestone"


# ===== 门控（不落回执的四种情形） =====


def test_no_receipt_when_undelivered():
    """未确认送达不结算 → 无回执（承接判定本身的门槛，回执继承）。"""
    sched, store = _make_scheduler()
    sched.record_sent(_UID, "stream-1", _NOW, "由头", event_id="ev_abc", origin="fragment")

    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=5)) is None
    assert _receipts(store) == []


def test_no_receipt_beyond_window():
    """超窗（16h）不结算 → 无回执（冷落口径归 settle_expired）。"""
    sched, store = _make_scheduler()
    _sign_delivered(sched, event_id="ev_abc", origin="fragment")

    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(hours=17)) is None
    assert _receipts(store) == []


def test_receipt_once_per_message():
    """每条主动消息至多结算一次 → 回执至多一条（consumed 语义继承）。"""
    sched, store = _make_scheduler()
    _sign_delivered(sched, event_id="ev_abc", origin="fragment")

    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=5)) is not None
    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=6)) is None
    assert len(_receipts(store)) == 1


def test_no_receipt_when_telemetry_disabled():
    """telemetry 开关关闭 → 静默不落（R17 已知耦合：采样总开关管全部指标）。"""
    sched, store = _make_scheduler(telemetry_enabled=False)
    _sign_delivered(sched, event_id="ev_abc", origin="fragment")

    assert sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=5)) is not None
    assert _receipts(store) == []


def test_resolve_catch_works_without_telemetry():
    """无 telemetry（旧测试夹具形态）：承接结算照常返回延迟，不崩。"""
    sched, _store = _make_scheduler(with_telemetry=False)
    _sign_delivered(sched, event_id="ev_abc", origin="fragment")

    latency = sched.resolve_catch(_UID, _NOW + datetime.timedelta(minutes=5))
    assert abs(latency - 5.0) < 1e-6


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
