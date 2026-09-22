"""由头返工 P1 测试（2026-09-22，issue-bysource-rework）。

覆盖三项：
- **B 去复用**：已用作由头的片段不再二次使用（kv `bysource:used:{user_id}`，按用户隔离）
- **C 质量门槛**：minor 档（无素材的纯状态切片）不单独作由头 → 宁可跳过本轮
- **G 承接结算**：`resolve_catch` 返回延迟分钟，每条主动消息至多结算一次

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_bysource_rework.py -q
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_ENGINE = _synth_loader.load("services.engine")
_SYNTH_SERVICES = _synth_loader.load("services")
_PROACTIVE = _synth_loader.load("services.proactive")

NarrativeEngine = _ENGINE.NarrativeEngine
ProactiveScheduler = _PROACTIVE.ProactiveScheduler

_NOW = datetime.datetime(2026, 9, 22, 12, 0, 0)


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeStore:
    """最小 store：kv + 事件 + 编年史接口。"""

    def __init__(self):
        self.kv_str: dict = {}
        self.kv_int: dict = {}
        self.events: list = []
        self.chronicle: list = []

    def list_events(self, scope, limit=20):
        return self.events[:limit]

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value

    def get_kv_int(self, key):
        return int(self.kv_int.get(key, 0))

    def set_kv_int(self, key, value):
        self.kv_int[key] = value

    def append_chronicle(self, scope, kind, text, ts=None):
        self.chronicle.append({"scope": scope, "kind": kind, "text": text, "ts": ts})

    def is_chronicle_done(self, scope, kind, date):
        return False

    def mark_chronicle_done(self, scope, kind, date):
        pass

    def recent_chronicle(self, scope, limit=3):
        return self.chronicle[:limit]


def _make_engine(pending):
    """构造含指定 pending_events 的 engine。"""
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            chronicle_enabled=True,
            mode_user_ids=["10001"],
            life_fragment_daily_max=6,
            life_fragment_interval_minutes=120,
            life_fragment_detail_enabled=True,
        ),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
        identity=SimpleNamespace(world="海边小城", values=[], world_rules=[], immutable_traits=[]),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=_Logger()))
    engine._store = _FakeStore()
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6, "last_shift_ts": ""},
            "routine": {"phase": "午后"},
            "focus": {"hot_thread": "", "pending_events": pending},
        }
    }
    branch = {
        "state": {"familiarity": 10.0, "milestones": []},
        "identity": {"stage": "陌生人"},
    }
    engine.load_branch_state = lambda uid: branch
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    return engine


def _frag(ts, tier, text="一段有画面的生活片段"):
    return {"ts": ts, "text": text, "tier": tier}


# ===== B 去复用 =====


def test_same_fragment_not_reused():
    """同一片段用过一次后，不再作第二次由头（宁可跳过，不重复说同一件事）。"""
    engine = _make_engine([_frag("2026-09-22T10:00:00", "normal")])

    first = engine.build_bysource("10001", _NOW)
    assert "一段有画面的生活片段" in first, "首次应取该片段"
    assert engine._store.get_kv_str("bysource:used:10001"), "用后应登记 ts（按 uid 隔离）"

    second = engine.build_bysource("10001", _NOW)
    assert second == "", "已用片段不应二次作由头（应跳过本轮）"


def test_two_fragments_rotated():
    """两条未用片段时，逐次取不同的（各自只用一次）。"""
    engine = _make_engine(
        [
            _frag("2026-09-22T10:00:00", "normal", "旧片段"),
            _frag("2026-09-22T11:00:00", "normal", "新片段"),
        ]
    )
    first = engine.build_bysource("10001", _NOW)
    second = engine.build_bysource("10001", _NOW)
    assert first != second, "连取两次应取到不同片段"
    assert third_is_empty(engine), "两条都用完后应无由头"


def test_used_marks_are_per_user():
    """去复用只约束**同一段关系**，不跨用户（同一件事讲给不同朋友听是自然的）。

    生活片段存在自我层（全局共享），7 位测试者共用同一批素材。若去重键不带 uid，
    先触发的用户会把素材耗尽，后触发的用户拿不到由头 → build_bysource 返回空
    → 本轮主动开口被跳过，触达面进一步收窄。
    """
    engine = _make_engine([_frag("2026-09-22T10:00:00", "normal")])

    first = engine.build_bysource("10001", _NOW)
    assert "一段有画面的生活片段" in first
    assert engine._store.get_kv_str("bysource:used:10001"), "应登记到甲自己的键"

    other = engine.build_bysource("10002", _NOW)
    assert "一段有画面的生活片段" in other, "跨用户不应互相耗尽素材"
    assert engine._store.get_kv_str("bysource:used:10002")

    assert engine.build_bysource("10001", _NOW) == "", "同一用户内去复用应仍然生效"


def third_is_empty(engine) -> bool:
    return engine.build_bysource("10001", _NOW) == ""


# ===== C 质量门槛 =====


def test_minor_fragment_not_used_alone():
    """minor 档（无素材的纯状态切片）太薄，不单独作由头 → 本轮跳过。"""
    engine = _make_engine([_frag("2026-09-22T10:00:00", "minor", "只是发了个呆")])
    assert engine.build_bysource("10001", _NOW) == "", "minor 档不应单独作由头"


def test_major_fragment_usable():
    """major 档可用（写细了的片段才值得开口）。"""
    engine = _make_engine([_frag("2026-09-22T10:00:00", "major", "写细了的那一件事")])
    bysource = engine.build_bysource("10001", _NOW)
    assert "写细了的那一件事" in bysource


def test_old_entry_without_tier_still_usable():
    """老片段没有 tier 字段（升级前写入）→ 按可用处理，不因缺字段丢失。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "升级前的片段"}])
    assert "升级前的片段" in engine.build_bysource("10001", _NOW)


# ===== G 迟来承接（24h） =====


def _make_scheduler():
    plugin = SimpleNamespace(
        config=SimpleNamespace(
            proactive=SimpleNamespace(
                random_minutes=[60, 240],
                default_active_window=["09:00-22:00"],
                user_window_rules=[],
            )
        ),
        ctx=SimpleNamespace(logger=_Logger()),
    )
    sched = ProactiveScheduler.__new__(ProactiveScheduler)
    sched._plugin = plugin
    sched._task = None
    sched._running = False
    sched._next_fire = {}
    sched._sent_records = {}
    sched._pending_at = {}
    return sched


def _sent_delivered(sched, uid="10001", stream="stream-1", ts=_NOW, bysource="由头"):
    """登记一次**已确认送达**的主动开口（承接判定的前置条件）。"""
    sched.record_sent(uid, stream, ts, bysource)
    sched.mark_delivered(stream)


def test_catch_returns_latency_minutes():
    """承接结算返回**延迟分钟**，而不是布尔值——口径在分析层切。"""
    sched = _make_scheduler()
    _sent_delivered(sched)

    latency = sched.resolve_catch("10001", _NOW + datetime.timedelta(hours=5))

    assert latency is not None, "窗口内应命中"
    assert abs(latency - 300.0) < 1e-6, f"延迟应为 300 分钟（实际 {latency}）"


def test_undelivered_is_not_catchable():
    """未确认送达的开口**不可被承接**——否则贪心归属会把用户随手发的消息错记成回应。

    离线回放实证（analysis/17-replay.txt）：不加这道门槛时承接率 50/44 = 114%，
    其中 16 条是错记；加上后 34/44 = 77%，与手算基准 78% 吻合。
    """
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头")  # 故意不 mark_delivered

    assert sched.resolve_catch("10001", _NOW + datetime.timedelta(minutes=5)) is None, (
        "没发出去的开口不该被算作被接住"
    )


def test_catch_only_once_per_message():
    """一条主动消息至多结算一次（取代 30min/24h 抢同一条的旧病）。"""
    sched = _make_scheduler()
    _sent_delivered(sched)

    assert sched.resolve_catch("10001", _NOW + datetime.timedelta(minutes=5)) is not None
    assert sched.resolve_catch("10001", _NOW + datetime.timedelta(minutes=6)) is None, (
        "同一条消息不应被结算第二次"
    )


def test_no_catch_beyond_window():
    """超窗（16 小时）不结算——由 settle_expired 走冷落/未送达口径。"""
    sched = _make_scheduler()
    _sent_delivered(sched)
    assert sched.resolve_catch("10001", _NOW + datetime.timedelta(hours=17)) is None


def test_catch_picks_latest_unconsumed():
    """多条主动消息：结算最近一条未被承接的，其余仍在窗口内可结算。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头甲")
    sched.record_sent("10001", "stream-1", _NOW + datetime.timedelta(minutes=10), "由头乙")
    sched.mark_delivered("stream-1")
    sched.mark_delivered("stream-1")

    # 第二次开口后 2 分钟回复 → 结算"由头乙"（延迟 2 分钟）
    latency = sched.resolve_catch("10001", _NOW + datetime.timedelta(minutes=12))
    assert abs(latency - 2.0) < 1e-6, f"应结算最新一条（实际 {latency}）"

    # 再由头甲仍在窗口内 → 下一条 inbound 结算它（延迟 20 分钟）
    latency2 = sched.resolve_catch("10001", _NOW + datetime.timedelta(minutes=20))
    assert abs(latency2 - 20.0) < 1e-6, f"应接着结算更早一条（实际 {latency2}）"

    assert sched.resolve_catch("10001", _NOW + datetime.timedelta(minutes=21)) is None


def test_mark_delivered_respects_grace_window():
    """宽限期外不认领：防止主动轮里 bot 连发多条时误标更早的开口。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头")

    assert sched.mark_delivered("stream-1", _NOW + datetime.timedelta(minutes=30)) is False, (
        "超出 10 分钟宽限期不应认领"
    )
    assert sched.mark_delivered("stream-1", _NOW + datetime.timedelta(minutes=3)) is True


def test_record_sent_tracks_single_record():
    """登记一次主动开口只产生**一条**记录（旧实现是两条并行队列）。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头")
    records = sched._sent_records.get("10001") or []
    assert len(records) == 1, f"应为单条记录（实际 {len(records)}）"
    assert records[0].delivered is False, "登记时尚未确认送达"
    assert records[0].consumed is False


def test_mark_delivered_flags_latest_for_stream():
    """送达确认打在该会话最近一条未确认的记录上。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头甲")
    sched.record_sent("10001", "stream-1", _NOW + datetime.timedelta(minutes=10), "由头乙")

    assert sched.mark_delivered("stream-1") is True
    records = sched._sent_records["10001"]
    assert records[0].delivered is False, "更早那条不应被误标"
    assert records[1].delivered is True, "应标在最新一条上"

    assert sched.mark_delivered("stream-1") is True, "次新那条仍待确认，应继续命中"
    assert records[0].delivered is True
    assert sched.mark_delivered("stream-1") is False, "全部确认过后不应再命中"


def test_settle_expired_ignored_only_when_delivered():
    """超窗结算：已送达无人接住 → 冷落；未送达 → undelivered，**不罚冷落**。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW - datetime.timedelta(hours=20), "甲")
    sched.record_sent("10001", "stream-1", _NOW - datetime.timedelta(hours=19), "乙")
    sched.mark_delivered("stream-1")  # 只确认了最新一条（乙）

    ignored, undelivered = sched.settle_expired("10001", _NOW)

    assert (ignored, undelivered) == (1, 1), f"应 1 冷落 + 1 未送达（实际 {ignored}, {undelivered}）"
    assert not sched._sent_records["10001"], "结算后应出队，下轮不重复惩罚"


def test_settle_expired_skips_consumed():
    """已被接住的记录超窗后不再计冷落。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW - datetime.timedelta(hours=18), "甲")
    sched.mark_delivered("stream-1")
    assert sched.resolve_catch("10001", _NOW - datetime.timedelta(hours=17)) is not None

    ignored, undelivered = sched.settle_expired("10001", _NOW)
    assert (ignored, undelivered) == (0, 0), "已承接的不应再罚"


def test_settle_expired_keeps_fresh():
    """窗口内的记录保留，不参与结算。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW - datetime.timedelta(hours=1), "甲")
    sched.mark_delivered("stream-1")

    assert sched.settle_expired("10001", _NOW) == (0, 0)
    assert len(sched._sent_records["10001"]) == 1, "窗口内记录应保留"


def test_trigger_accepted():
    """触发返回值判定：正常排队算成功，success=False / 缺 task_id 算被拒。"""
    assert _PROACTIVE._trigger_accepted({"queued": True, "task_id": "t1"}) is True
    assert _PROACTIVE._trigger_accepted({"success": False}) is False
    assert _PROACTIVE._trigger_accepted({}) is False
    assert _PROACTIVE._trigger_accepted(None) is False


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
