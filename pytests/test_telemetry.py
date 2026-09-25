"""Telemetry 采样判定单测（2026-09-13 体检 C5：判定逻辑自 plugin.py hook 下沉）。

口径与下沉前逐条对齐：
- 指标 1 user_initiated_freq：距上次出站 > 5 分钟才算用户主动发起；
- 指标 2 rounds：出站在入站 30 分钟窗口内到达配对 1 轮，user_id 随行；
- 指标 2 user_msg_len/bot_msg_len：按消息长度记录；
- 关键纪律：跟踪状态（_pending_round/_last_bot_sent）更新**无条件**，
  telemetry.enabled=false 时只是不写 CSV（A/B 对照窗口依赖此语义）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_telemetry.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_telemetry.py
"""

from __future__ import annotations

import datetime
import logging
import sys
from types import SimpleNamespace

import _synth_loader

_TELEMETRY_MOD = _synth_loader.load("services.state.snapshot")

Telemetry = _TELEMETRY_MOD.Telemetry

_LOGGER = logging.getLogger("telemetry-test")
_NOW = datetime.datetime(2026, 9, 13, 12, 0, 0)


class _FakeStore:
    """捕获 append_metric 调用的假 store。"""

    def __init__(self) -> None:
        self.metric_rows: list = []

    def append_metric(self, name, value, user_id="", scope="", ts=None) -> None:
        self.metric_rows.append(
            SimpleNamespace(name=name, value=value, user_id=user_id, scope=scope)
        )


def _make_telemetry(*, telemetry_enabled: bool = True) -> tuple:
    store = _FakeStore()
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        telemetry=SimpleNamespace(enabled=telemetry_enabled),
    )
    telemetry = Telemetry(
        SimpleNamespace(config=cfg, _store=store, ctx=SimpleNamespace(logger=_LOGGER))
    )
    return telemetry, store


def _rows(store, scope: str) -> list:
    return [r for r in store.metric_rows if r.scope == scope]


def test_inbound_registers_round_and_user_initiated():
    """入站：登记待配对轮次；距上次出站 > 5 分钟记用户主动发起 + 入站长度。"""
    telemetry, store = _make_telemetry()

    telemetry.note_inbound(stream_id="s1", user_id="u1", text="你好呀", now=_NOW)

    assert "s1" in telemetry._pending_round, "入站应登记待配对轮次"
    initiated = [r for r in store.metric_rows if r.name == "user_initiated_freq"]
    assert len(initiated) == 1, "无出站历史（epoch 起算）应视为用户主动发起"
    assert initiated[0].user_id == "u1"
    lens = _rows(store, "user_msg_len")
    assert len(lens) == 1 and lens[0].value == 3.0


def test_inbound_after_recent_bot_reply_not_user_initiated():
    """bot 刚回复过（< 5 分钟）→ 入站不算用户主动发起。"""
    telemetry, store = _make_telemetry()
    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "我来说"}]},
        now=_NOW - datetime.timedelta(minutes=2),
    )
    store.metric_rows.clear()

    telemetry.note_inbound(stream_id="s1", user_id="u1", text="嗯", now=_NOW)

    assert not [r for r in store.metric_rows if r.name == "user_initiated_freq"], (
        "距 bot 出站仅 2 分钟，不应记用户主动发起"
    )


def test_outbound_pairs_round_within_window():
    """出站：窗口内的待配对入站 → 1 轮，user_id 随行，待办消费。"""
    telemetry, store = _make_telemetry()
    telemetry.note_inbound(
        stream_id="s1", user_id="u1", text="在吗",
        now=_NOW - datetime.timedelta(minutes=10),
    )

    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "我在"}]},
        now=_NOW,
    )

    rounds = _rows(store, "rounds")
    assert len(rounds) == 1 and rounds[0].value == 1 and rounds[0].user_id == "u1"
    assert "s1" not in telemetry._pending_round
    # bot 长度按 text 组件求和
    bot = _rows(store, "bot_msg_len")
    assert len(bot) == 1 and bot[0].value == 2.0


def test_outbound_expired_round_not_paired():
    """入站超 30 分钟窗口 → 不配对，但待办仍被消费。"""
    telemetry, store = _make_telemetry()
    telemetry._pending_round["s1"] = _NOW - datetime.timedelta(minutes=40)

    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "hi"}]},
        now=_NOW,
    )

    assert not _rows(store, "rounds")
    assert "s1" not in telemetry._pending_round


def test_state_updates_unconditional_when_disabled():
    """telemetry.enabled=false：不写 CSV，但轮次登记/出站时刻照常更新（纪律用例）。"""
    telemetry, store = _make_telemetry(telemetry_enabled=False)

    telemetry.note_inbound(stream_id="s1", user_id="u1", text="你好", now=_NOW)
    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "嗯"}]},
        now=_NOW + datetime.timedelta(minutes=1),
    )

    assert store.metric_rows == [], "采样关闭时不得写 CSV"
    assert "s1" not in telemetry._pending_round, "待办应照常被出站消费"
    assert "s1" in telemetry._last_bot_sent, "出站时刻应照常登记"


def test_outbound_without_stream_still_records_length():
    """无 stream_id 的出站：不更新轮次状态，但 bot 长度仍记录（与原 hook 一致）。"""
    telemetry, store = _make_telemetry()

    telemetry.note_outbound(
        stream_id="", user_id="",
        message={"raw_message": [{"type": "text", "data": "广播"}]},
        now=_NOW,
    )

    assert telemetry._last_bot_sent == {}
    bot = _rows(store, "bot_msg_len")
    assert len(bot) == 1 and bot[0].value == 2.0


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
