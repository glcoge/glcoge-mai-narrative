"""主动素材注入测试（单点解冻 #2 / 复读修复方案 A，2026-10-11）。

背景（Gi Hitomi 案定谳）：旧由头把片段全文塞进 reason，并硬指示「取最近一条
用户消息的 msg_id」——静默私聊里该锚点是数小时前已答过的旧提问（或被 planner
抓成 bot 自己的消息），replyer 被目标消息指令框死 → 重答旧提问 / 原句复读。
新设计（参考 idle_proactive_chat / mai-life）：
- 片段全文经 ``maisaka.append_context`` 注入为纯文本素材——宿主渲染不带
  msg_id 前缀且 ``original_message=None``，reply 选不中它，素材与锚点解耦；
- reason 只留意图边界（分享自己的事 ≠ 回答旧提问；可以不说）+ msg_id
  软引导（发送锚语义；2026-09-22 漏传教训保留引导，去掉误导性的「最近一条」）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_proactive_material_inject.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from types import SimpleNamespace
from typing import Any, Dict, List

import _synth_loader

sched_mod = _synth_loader.load("services.proactive.scheduler")

_NOW = datetime.datetime(2026, 10, 11, 15, 0)
_UID = "10000"
_STREAM = "stream-10000"
_EVENT_ID = "ev_20261011080000_deadbeef"
_ORIGIN = "fragment"
_FRAGMENT = "手机屏幕亮了又暗。什么可爱小柯伊，恶心死了。"


def _make_plugin(
    monkeypatch,
    *,
    append_exc: Exception | None = None,
    append_result: Any = None,
    trigger_result: Any = None,
    ext_enabled: bool = False,
):
    calls: Dict[str, List[Any]] = {
        "seq": [],
        "append": [],
        "trigger": [],
        "metrics": [],
        "topic": [],
    }

    async def fake_append(stream_id, segments, **kwargs):
        calls["seq"].append("append")
        calls["append"].append((stream_id, segments, kwargs))
        if append_exc is not None:
            raise append_exc
        return append_result if append_result is not None else {"success": True}

    async def fake_trigger(stream_id, *, intent="", reason="", metadata=None, **kwargs):
        calls["seq"].append("trigger")
        calls["trigger"].append(
            {"stream_id": stream_id, "intent": intent, "reason": reason, "metadata": metadata}
        )
        # _trigger_accepted 要求 queued/task_id（success:True 单独不算接受）
        return trigger_result if trigger_result is not None else {
            "success": True,
            "queued": True,
            "task_id": "task-1",
        }

    store = _synth_loader.FakeStore()
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        proactive=SimpleNamespace(reply_extension_enabled=ext_enabled),
    )
    plugin = SimpleNamespace(
        config=cfg,
        _engine=SimpleNamespace(deps=SimpleNamespace(store=store)),
        _telemetry=SimpleNamespace(
            record=lambda name, value=1, user_id="", scope="": calls["metrics"].append(name)
        ),
        _store=store,
        _local_now=lambda: _NOW,
        reply_extension_full_name="glcoge.mai-narrative.proactive_bysource",
        ctx=SimpleNamespace(
            logger=_synth_loader.make_logger(),
            maisaka=SimpleNamespace(
                append_context=fake_append,
                proactive=SimpleNamespace(trigger=fake_trigger),
            ),
        ),
    )
    # ⚠️ load() 每次重新执行模块：必须打在 scheduler 自己绑定的 sourcing 对象上，
    # 单独 load 一份 sourcing 打补丁不会生效（B0 基建地雷的老坑）。
    monkeypatch.setattr(
        sched_mod.sourcing,
        "build_bysource_detail",
        lambda deps, uid, now: {"text": _FRAGMENT, "event_id": _EVENT_ID, "origin": _ORIGIN},
    )
    scheduler = _synth_loader.make_scheduler(plugin)
    # ⚠️ make_scheduler 内部重新 load 一份 scheduler 模块（M2）：note_pending_topic
    # 是 M2 命名空间的名字绑定，补丁必须打在实例所属模块上（批 3 老方子）。
    sched_real = sys.modules[type(scheduler).__module__]
    monkeypatch.setattr(
        sched_real, "note_pending_topic", lambda store, uid, text: calls["topic"].append((uid, text))
    )
    return scheduler, plugin, calls


def test_fire_injects_material_before_trigger(monkeypatch):
    """素材注入先于 trigger，携带事件署名 message_id 与插件 source_kind。"""
    scheduler, plugin, calls = _make_plugin(monkeypatch)
    asyncio.run(scheduler._fire(_UID, _STREAM, _NOW))

    assert calls["seq"] == ["append", "trigger"]
    assert len(calls["append"]) == 1
    stream_id, segments, kwargs = calls["append"][0]
    assert stream_id == _STREAM
    assert kwargs["message_id"] == f"narrative:share:{_EVENT_ID}"
    assert kwargs["source_kind"] == "plugin:glcoge.mai-narrative"
    assert kwargs["visible_text"]
    assert len(segments) == 1 and segments[0]["type"] == "text"
    # 素材 = 签发事件原文（保 bysource:used 回执对账），带防混淆标记
    assert _FRAGMENT in segments[0]["content"]
    assert "素材" in segments[0]["content"]
    assert "不是用户发言" in segments[0]["content"]


def test_reason_is_guidance_not_material(monkeypatch):
    """reason 不含片段全文与旧硬指示；含意图边界与 msg_id 软引导。"""
    scheduler, plugin, calls = _make_plugin(monkeypatch)
    asyncio.run(scheduler._fire(_UID, _STREAM, _NOW))

    assert len(calls["trigger"]) == 1
    fired = calls["trigger"][0]
    assert fired["intent"] == "按剧本生活主动开口"
    reason = fired["reason"]
    assert _FRAGMENT not in reason
    assert "必须调用 reply" not in reason
    assert "最近一条用户消息" not in reason
    assert "发送锚" in reason
    assert "不要用柯伊自己发的消息" in reason
    assert "不是去回答对方以前的提问" in reason
    assert "可以不说" in reason


def test_append_failure_skips_round(monkeypatch):
    """append_context 抛异常 → 不触发、不计数、记指标（素材不在场不开口）。"""
    scheduler, plugin, calls = _make_plugin(monkeypatch, append_exc=RuntimeError("rpc down"))
    asyncio.run(scheduler._fire(_UID, _STREAM, _NOW))

    assert calls["trigger"] == []
    assert "proactive_material_inject_failed" in calls["metrics"]
    today = _NOW.strftime("%Y-%m-%d")
    assert plugin._store.get_kv_int(f"proactive:count:{_UID}:{today}") == 0


def test_append_rejected_skips_round(monkeypatch):
    """append_context 返回 success=False → 同样跳过本轮。"""
    scheduler, plugin, calls = _make_plugin(
        monkeypatch, append_result={"success": False, "error": "stream missing"}
    )
    asyncio.run(scheduler._fire(_UID, _STREAM, _NOW))

    assert calls["trigger"] == []
    assert "proactive_material_inject_failed" in calls["metrics"]


def test_ext_hint_still_appended(monkeypatch):
    """REPLY_EXTENSION 教学（批 4 / R39）在新 reason 上照常拼接。"""
    scheduler, plugin, calls = _make_plugin(monkeypatch, ext_enabled=True)
    asyncio.run(scheduler._fire(_UID, _STREAM, _NOW))

    reason = calls["trigger"][0]["reason"]
    assert "plugin_options" in reason
    assert "glcoge.mai-narrative.proactive_bysource" in reason
    assert "发送锚" in reason  # 软引导未被 hint 拼接破坏


def test_receipt_bookkeeping_intact(monkeypatch):
    """簿记链零变更：record_sent 仍带片段原文与事件署名，话题归因照记。"""
    scheduler, plugin, calls = _make_plugin(monkeypatch)
    asyncio.run(scheduler._fire(_UID, _STREAM, _NOW))

    records = scheduler._sent_records[_UID]
    assert len(records) == 1
    assert records[0].bysource == _FRAGMENT
    assert records[0].event_id == _EVENT_ID
    assert records[0].origin == _ORIGIN
    assert scheduler._pending_at[_STREAM]["bysource"] == _FRAGMENT
    assert calls["topic"] == [(_UID, _FRAGMENT)]
    today = _NOW.strftime("%Y-%m-%d")
    assert plugin._store.get_kv_int(f"proactive:count:{_UID}:{today}") == 1
