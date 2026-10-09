"""engaged 里程碑测试（第④步 / 方案 §6 / Q4 / Q9 / Q10 / Q16 / Q17）。

覆盖三条链：
- **写入链**：承接命中开窗（``note_engaged``）→ ≥3 条且 ≥30 字 → ``append_milestone``
  落一条原文条目；「嗯」式敷衍、短句堆砌、窗过期、开关关闭一律不落。
- **消费链**：``build_bysource`` 的里程碑三闸（7 天条目冷却 / 30 天保质 / 3 选 1 节流）
  + 跨用户隔离（desc 是用户原话，永不进别人的上下文）。
- **副作用**：第一条里程碑把 stage 由「陌生人」抬到「相识」（§6.4，已知并接受）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_engaged_milestone.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_engaged_milestone.py
"""

from __future__ import annotations

import datetime
import json
import sys
from types import SimpleNamespace

import _synth_loader

_ENGINE_MOD = _synth_loader.load("services.state.engine")
continuity = _synth_loader.load("services.state.continuity")
_SCHEDULER = _synth_loader.load("services.proactive.scheduler")
_SOURCING = _synth_loader.load("services.proactive.sourcing")
_CONTINUITY = _synth_loader.load("services.state.continuity")

NarrativeEngine = _ENGINE_MOD.NarrativeEngine
ProactiveScheduler = _SCHEDULER.ProactiveScheduler
build_bysource = _SOURCING.build_bysource
current_relationship_stage = _CONTINUITY.current_relationship_stage

_NOW = datetime.datetime(2026, 9, 22, 11, 37, 0)
_UID = "10001"
_OTHER = "20002"


class _Logger:
    def __init__(self):
        self.messages: list = []

    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        self.messages.append(str(a[0] % a[1:]) if len(a) > 1 else str(a[0]))

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _make_engine(*, milestone_enabled=True, milestones=None, uid=_UID):
    """真 engine 实例 + 假 store + 内存 branch（``append_milestone`` 走真实现）。"""
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(enabled=True, mode_user_ids=[uid], fragment_pending_max=12),
        proactive=SimpleNamespace(milestone_enabled=milestone_enabled),
    )
    branch = {
        "meta": {},
        "relationship": {"milestones": list(milestones or [])},
        "state": {},
    }
    logger = _Logger()
    store = _synth_loader.FakeStore()
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=logger), _store=store)
    engine._store = store
    engine._local_now = lambda: _NOW
    engine.load_branch_state = lambda _uid: branch
    engine.save_branch_state = lambda _uid, _state: None
    engine._branch = branch
    engine._test_logger = logger
    return engine


def _make_scheduler(engine):
    """真 scheduler 实例（六件套经 _synth_loader.make_scheduler 初始化；不开 asyncio 循环）。"""
    plugin = SimpleNamespace(
        _engine=engine,
        _store=engine._store,
        _local_now=lambda: _NOW,
        ctx=SimpleNamespace(logger=engine._test_logger),
    )
    return _synth_loader.make_scheduler(plugin)


def _note(sched, text, minute=0, *, catch=False):
    return sched.note_engaged(_UID, text, _NOW + datetime.timedelta(minutes=minute), catch=catch)


def _milestones(engine):
    return engine._branch["relationship"]["milestones"]


# ===== 写入链：engaged 判定（Q9=c） =====


def test_single_ack_not_engaged():
    """「嗯」式敷衍不落库：replies=1 / chars=1，两条也拿不下 30 字门槛（Q9 的目的）。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    assert _note(sched, "嗯", 0, catch=True) is False
    assert _milestones(engine) == []


def test_three_short_replies_not_engaged():
    """条数够（3 条）但合计不足 30 字 → 不落（「取且」的「且」这一半）。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "嗯", 0, catch=True)
    _note(sched, "好", 5)
    _note(sched, "行吧", 10)
    assert _milestones(engine) == [], "3 条共 4 字不应算「聊起来了」"


def test_chars_alone_not_engaged():
    """字数够但条数不足（1 条长文）→ 不落（「取且」的「且」另一半）。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    assert _note(sched, "这是一条特别长的消息" * 5, 0, catch=True) is False
    assert _milestones(engine) == []


def test_engaged_appends_one_milestone():
    """达标（3 条且 ≥30 字）→ 落一条，形状 id/ts/desc 齐全、desc 是原话拼接。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "昨晚那个梦我也做过，梦见在老家的河边走着", 0, catch=True)
    _note(sched, "河边还有小时候那种石头台阶，水特别清", 8)
    assert _milestones(engine) == [], "只有 2 条时不该落"
    assert _note(sched, "后来醒了还愣了半天才反应过来是梦", 14) is True

    milestones = _milestones(engine)
    assert len(milestones) == 1
    entry = milestones[0]
    assert entry["id"].startswith("engaged:"), "id 必须避开 stage: 前缀（否则劫持阶段推导）"
    assert entry["ts"] == (_NOW + datetime.timedelta(minutes=14)).isoformat(timespec="seconds")
    assert "昨晚那个梦我也做过" in entry["desc"]
    assert "后来醒了还愣了半天" in entry["desc"]
    assert "\n" in entry["desc"], "多条原话按行拼接"


def test_engaged_logged_at_info():
    """落库打 INFO（L0-3 观察点靠 grep 计数）。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "第一条写点足够长的话凑字数", 0, catch=True)
    _note(sched, "第二条也写点足够长的话", 3)
    _note(sched, "第三条还是写点足够长的话", 6)
    assert any("engaged 里程碑落库" in msg for msg in engine._test_logger.messages)


def test_desc_truncated_to_300():
    """desc 截断 300 字（Q16）：只存原文、不存总结，但要防单条过长。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "甲" * 200, 0, catch=True)
    _note(sched, "乙" * 200, 1)
    _note(sched, "丙" * 200, 2)
    assert len(_milestones(engine)[0]["desc"]) == 300


def test_one_milestone_per_window():
    """一次 engaged 只落一条（Q16）：达标即销毁窗口，后续消息不再追加。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "第一条写点足够长的话凑够字数", 0, catch=True)
    _note(sched, "第二条也写点足够长的话", 2)
    _note(sched, "第三条还是写点足够长的话", 4)
    assert len(_milestones(engine)) == 1
    _note(sched, "第四条继续聊下去也不该再落一条", 10)
    _note(sched, "第五条更是如此，窗已经作废了", 12)
    assert len(_milestones(engine)) == 1, "窗口销毁后普通消息不得重新开窗"


def test_no_window_no_counting():
    """没有承接就没有窗：普通消息（catch=False）不自行开窗，也不计数。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    for minute in range(6):
        assert _note(sched, "随口聊聊第一条足够长的内容", minute) is False
    assert _milestones(engine) == []
    assert sched._engaged_windows == {}


def test_window_expires_after_60min():
    """窗超过 60 分钟后到达的消息不计入（入口过期兜底，与 tick 回收同条件）。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "承接后的第一条，内容写得长一些", 0, catch=True)
    # 61 分钟后：旧窗作废；这条不构成新窗（catch=False）→ 不落
    assert _note(sched, "一个多小时后才回的一条消息", 61) is False
    assert _milestones(engine) == []
    # 但若此时又有一次承接（catch=True），应开**新窗**且只计这一条
    _note(sched, "新一次承接的第一条消息内容", 62, catch=True)
    assert len(_milestones(engine)) == 0
    assert sched._engaged_windows[_UID].replies == 1


def test_prune_runs_without_sent_records():
    """``settle_expired`` 在无开场记录（早退分支）时也必须回收 engaged 窗。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    sched._engaged_windows[_UID] = _SCHEDULER._EngagedWindow(
        opened=_NOW - datetime.timedelta(minutes=61)
    )
    assert sched.settle_expired(_UID, _NOW) == (0, 0)
    assert _UID not in sched._engaged_windows, "早退分支必须在 prune 之后"


def test_clear_sent_clears_windows():
    """状态重置同时清空 engaged 窗。"""
    engine = _make_engine()
    sched = _make_scheduler(engine)
    _note(sched, "承接后的第一条，内容写得长一些", 0, catch=True)
    sched.clear_sent()
    assert sched._engaged_windows == {}


def test_milestone_disabled_no_write():
    """``milestone_enabled=false`` 只停写：达标返回 False、库中不落条目。"""
    engine = _make_engine(milestone_enabled=False)
    sched = _make_scheduler(engine)
    _note(sched, "第一条写点足够长的话凑够字数", 0, catch=True)
    _note(sched, "第二条也写点足够长的话", 2)
    assert _note(sched, "第三条还是写点足够长的话", 4) is False
    assert _milestones(engine) == []


def test_milestone_keep_bounded_20():
    """每用户有界 20 条：超出丢最旧。"""
    engine = _make_engine()
    for index in range(21):
        continuity.append_milestone(engine.deps,_UID, f"第 {index} 次聊起来的内容", _NOW + datetime.timedelta(minutes=index))
    milestones = _milestones(engine)
    assert len(milestones) == 20
    assert "第 0 次" not in milestones[0]["desc"], "最旧的应被挤出"
    assert "第 20 次" in milestones[-1]["desc"]


def test_first_milestone_raises_stage():
    """副作用（§6.4）：第一条里程碑让 stage 由「陌生人」→「相识」。"""
    engine = _make_engine()
    assert current_relationship_stage({"milestones": _milestones(engine)}) == "陌生人"
    continuity.append_milestone(engine.deps,_UID, "第一次真的聊起来", _NOW)
    assert current_relationship_stage({"milestones": _milestones(engine)}) == "相识"


# ===== 消费链：里程碑三闸 + 隔离（Q10b / §6.5） =====


def _iso(days_ago: float = 0.0) -> str:
    return (_NOW - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")


def _make_sourcing_engine(branch_by_uid, pending=None):
    """sourcing 夹具：按 uid 返回不同 branch（验隔离），pending 为空即只考里程碑路。"""
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(enabled=True, mode_user_ids=[_UID], fragment_pending_max=12),
        proactive=SimpleNamespace(milestone_enabled=True),
    )
    logger = _Logger()
    store = _synth_loader.FakeStore()
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=logger), _store=store)
    engine._store = store
    engine._local_now = lambda: _NOW
    engine.load_self_state = lambda: {
        "state": {
            "mood": {"label": "平静", "energy": 0.6, "last_shift_ts": ""},
            "routine": {"phase": "午后"},
            "focus": {"pending_events": list(pending or [])},
            "urge": 0.5,
        }
    }
    engine.load_branch_state = lambda uid: branch_by_uid.get(uid, {"relationship": {}})
    engine.save_branch_state = lambda uid, state: None
    engine.save_self_state = lambda state: None
    return engine


def _branch_with(milestones, *, stage=""):
    relationship = {"milestones": list(milestones)}
    if stage:
        relationship["stage"] = stage
    return {"meta": {}, "relationship": relationship, "state": {}}


def test_milestone_usable_when_fresh():
    """对照基线：新条目、无冷却、来源队列无 milestone → 正常作由头。"""
    milestone = {"id": f"engaged:{_iso(1)}", "ts": _iso(1), "desc": "河边石头台阶那件事"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    chosen = build_bysource(engine.deps, _UID, _NOW)
    assert "河边石头台阶那件事" in chosen


def test_milestone_ttl_blocks_stale():
    """① 保质期 30 天：过期旧事不再提取（宁可缺席，也不翻旧账）。"""
    milestone = {"id": f"engaged:{_iso(31)}", "ts": _iso(31), "desc": "三十一天前的旧事"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    assert build_bysource(engine.deps, _UID, _NOW) == ""


def test_milestone_cooldown_blocks_recent():
    """② 条目冷却 7 天：同一件事短期内不反复拿来讲。"""
    ts = _iso(2)
    milestone = {"id": f"engaged:{ts}", "ts": ts, "desc": "前两天刚讲过的那件事"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    engine._store.set_kv_str(
        f"{_SOURCING._MILESTONE_USED_KEY_PREFIX}{_UID}",
        json.dumps({milestone["id"]: _iso(3)}),  # 3 天前消费过 → < 7 天
    )
    assert build_bysource(engine.deps, _UID, _NOW) == ""


def test_milestone_cooldown_expired_allows():
    """冷却满 7 天后旧事可以再讲（防「一挡到底」）。"""
    ts = _iso(10)
    milestone = {"id": f"engaged:{ts}", "ts": ts, "desc": "十几天前那件事"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    engine._store.set_kv_str(
        f"{_SOURCING._MILESTONE_USED_KEY_PREFIX}{_UID}",
        json.dumps({milestone["id"]: _iso(8)}),  # 8 天前消费过 → 冷却已满
    )
    assert "十几天前那件事" in build_bysource(engine.deps, _UID, _NOW)


def test_milestone_throttle_one_in_three():
    """③ 3 选 1 节流：最近 3 次由头里已有 milestone → 本轮不取回忆。"""
    milestone = {"id": f"engaged:{_iso(1)}", "ts": _iso(1), "desc": "上次讲过的那件事"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    engine._store.set_kv_str(
        f"{_SOURCING._ORIGIN_KEY_PREFIX}{_UID}", "fragment,milestone,fragment"
    )
    assert build_bysource(engine.deps, _UID, _NOW) == ""

    # 队列里 milestone 滑出窗口（3 次内没有了）→ 重新可取
    engine._store.set_kv_str(f"{_SOURCING._ORIGIN_KEY_PREFIX}{_UID}", "fragment,mood,fragment")
    assert "上次讲过的那件事" in build_bysource(engine.deps, _UID, _NOW)


def test_milestone_select_registers_cooldown_and_origin():
    """选中即登记（Q13）：条目冷却 kv + 来源队列都写入，下一轮被自身冷却挡住。"""
    ts = _iso(1)
    milestone = {"id": f"engaged:{ts}", "ts": ts, "desc": "刚刚取用过的那件事"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    assert "刚刚取用过的那件事" in build_bysource(engine.deps, _UID, _NOW)

    consumed = json.loads(engine._store.get_kv_str(f"{_SOURCING._MILESTONE_USED_KEY_PREFIX}{_UID}"))
    assert consumed[milestone["id"]] == _NOW.isoformat(timespec="seconds")
    origins = engine._store.get_kv_str(f"{_SOURCING._ORIGIN_KEY_PREFIX}{_UID}")
    assert origins.split(",")[-1] == "milestone"
    # 同一轮之后立刻再取 → 冷却挡住（>0 天 <7 天）
    assert build_bysource(engine.deps, _UID, _NOW) == ""


def test_origin_registered_for_fragment_path():
    """非里程碑来源同样进来源队列（3 选 1 的分母）。"""
    engine = _make_sourcing_engine(
        {_UID: _branch_with([])},
        pending=[{"ts": _iso(0), "text": "今天去海边走了走", "highlight": False}],
    )
    assert "今天去海边走了走" in build_bysource(engine.deps, _UID, _NOW)
    origins = engine._store.get_kv_str(f"{_SOURCING._ORIGIN_KEY_PREFIX}{_UID}")
    assert origins.split(",")[-1] == "fragment"


def test_milestone_isolated_per_user():
    """跨用户隔离（§6.5）：desc 是用户原话，永不进别人的由头。"""
    mine = {"id": f"engaged:{_iso(1)}", "ts": _iso(1), "desc": "我说过的那件私事"}
    theirs = {"id": f"engaged:{_iso(1)}", "ts": _iso(1), "desc": "别人说过的那件私事"}
    engine = _make_sourcing_engine(
        {_UID: _branch_with([mine]), _OTHER: _branch_with([theirs])}
    )
    chosen = build_bysource(engine.deps, _UID, _NOW)
    assert "我说过的那件私事" in chosen
    assert "别人说过的那件私事" not in chosen

    chosen_other = build_bysource(engine.deps, _OTHER, _NOW)
    assert "别人说过的那件私事" in chosen_other
    assert "我说过的那件私事" not in chosen_other


def test_milestone_skipped_when_stranger():
    """stage 门槛保留（Q10c）：无 stage 且 milestones 为空时是陌生人 → 不取回忆。"""
    engine = _make_sourcing_engine({_UID: _branch_with([])})
    assert build_bysource(engine.deps, _UID, _NOW) == ""


def test_bad_ts_fail_closed():
    """ts 解析不出的条目一律跳过（fail-closed）：不拿捏不准的时间做减法。"""
    milestone = {"id": "engaged:? ", "ts": "不是时间", "desc": "时间戳坏掉的条目"}
    engine = _make_sourcing_engine({_UID: _branch_with([milestone])})
    assert build_bysource(engine.deps, _UID, _NOW) == ""


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
