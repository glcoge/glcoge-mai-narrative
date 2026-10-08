"""由头返工 P1 测试（2026-09-22，issue-bysource-rework）。

覆盖三项：
- **B 去复用**：已用作由头的片段不再二次使用（kv `bysource:used:{user_id}`，按用户隔离）
- **C 资格解耦**（2026-09-30 回滚批 1）：片段不论档位都可作由头（原 minor 排除已撤销，Q1）
- **G 承接结算**：`resolve_catch` 返回延迟分钟，每条主动消息至多结算一次

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_bysource_rework.py -q
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader
from pytests._synth_loader import FakeStore, make_logger  # noqa: E402

_ENGINE = _synth_loader.load("services.state.engine")
_SYNTH_SERVICES = _synth_loader.load("services")
_PROACTIVE = _synth_loader.load("services.proactive.scheduler")
_SOURCING = _synth_loader.load("services.proactive.sourcing")

NarrativeEngine = _ENGINE.NarrativeEngine
ProactiveScheduler = _PROACTIVE.ProactiveScheduler

_NOW = datetime.datetime(2026, 9, 22, 12, 0, 0)




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
            fragment_pending_max=12,
        ),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
        identity=SimpleNamespace(world="海边小城", values=[], world_rules=[], immutable_traits=[]),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    # plugin._store 与 engine._store 在生产里是**同一个实例**（scheduler 走前者、
    # engine 走后者）；批 3-C5 话题归因从 plugin 侧取 store，假对象必须同样共享
    store = FakeStore()
    engine._plugin = SimpleNamespace(
        config=config, ctx=SimpleNamespace(logger=make_logger()), _store=store
    )
    engine._store = store
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6, "last_shift_ts": ""},
            "routine": {"phase": "午后"},
            "focus": {"pending_events": pending},
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
    """去复用只约束**同一段关系**，kv 键按 uid 隔离（同一件事讲给不同朋友听是自然的）。

    生活片段存在自我层（全局共享），7 位测试者共用同一批素材。若去重键不带 uid，
    先触发的用户会把素材耗尽，后触发的用户拿不到由头 → build_bysource 返回空
    → 本轮主动开口被跳过，触达面进一步收窄。

    批 2（R44）语义变更：签发冷却（默认 6h）内同一事件不签给第二人——跨用户的
    素材共享观察窗移到冷却结束之后（used 键的 per-uid 隔离由分键本身保证）。
    """
    engine = _make_engine([_frag("2026-09-22T10:00:00", "normal")])

    first = engine.build_bysource("10001", _NOW)
    assert "一段有画面的生活片段" in first
    assert engine._store.get_kv_str("bysource:used:10001"), "应登记到甲自己的键"

    other = engine.build_bysource("10002", _NOW + datetime.timedelta(hours=7))
    assert "一段有画面的生活片段" in other, "冷却窗外跨用户可共享素材"
    assert engine._store.get_kv_str("bysource:used:10002"), "乙登记到乙自己的键"

    assert engine.build_bysource("10001", _NOW) == "", "同一用户内去复用应仍然生效"


def third_is_empty(engine) -> bool:
    return engine.build_bysource("10001", _NOW) == ""


# ===== C 资格解耦（2026-09-30 回滚批 1）=====


def test_minor_fragment_usable_alone():
    """回滚批 1 的 minor 排除（Q1）：纯状态切片同样可作由头。"""
    engine = _make_engine([_frag("2026-09-22T10:00:00", "minor", "只是发了个呆")])
    assert "只是发了个呆" in engine.build_bysource("10001", _NOW)


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
        ctx=SimpleNamespace(logger=make_logger()),
        # 批 3-C5：resolve_catch 接住时会给话题加权，需要 plugin._store
        _store=FakeStore(),
    )
    sched = ProactiveScheduler.__new__(ProactiveScheduler)
    sched._plugin = plugin
    sched._task = None
    sched._running = False
    sched._next_fire = {}
    sched._sent_records = {}
    sched._pending_at = {}
    # engaged 计数窗（第④步新增；__new__ 绕过 __init__ 故须手工初始化）
    sched._engaged_windows = {}
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


# ===== 批 0（v0.3.0 / R41）：build_bysource_detail + used 键位泛化 =====


def test_detail_matches_bysource_output():
    """零行为 diff：detail 与旧接口同状态同选择、text 逐字节一致，且带 origin/event_id。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "一段有画面的生活片段"}])

    detail = engine.build_bysource_detail("10001", _NOW)
    assert detail is not None
    assert "一段有画面的生活片段" in detail["text"]
    assert detail["origin"] == "fragment"
    assert detail["event_id"], "新条目应有事件实体 id"

    # 同一状态重放旧接口：选择一致（event_id 确定性 ⇒ used 登记同键 ⇒ 同跳过链）
    engine2 = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "一段有画面的生活片段"}])
    assert engine2.build_bysource("10001", _NOW) == detail["text"]


def test_detail_none_when_no_candidates():
    """无可借素材：detail 返回 None、旧接口返回空串（跳过本轮，不干聊）。"""
    engine = _make_engine([])
    assert engine.build_bysource_detail("10001", _NOW) is None
    assert engine.build_bysource("10001", _NOW) == ""


def test_new_entry_registered_by_event_id():
    """新条目（带 event_id）签发后 used 登记的是 event_id，不是 ts。"""
    entry = {
        "ts": "2026-09-22T10:00:00",
        "text": "一段有画面的生活片段",
        "event_id": "ev_20260922100000_abcd1234",
        "kind": "fragment",
    }
    engine = _make_engine([entry])
    assert engine.build_bysource("10001", _NOW)

    used = engine._store.get_kv_str("bysource:used:10001")
    assert "ev_20260922100000_abcd1234" in used, "应登记 event_id"
    assert "2026-09-22T10:00:00" not in used, "不应再登记 ts（键位已泛化）"


def test_legacy_entry_still_registered_by_ts():
    """旧条目（无 event_id）签发后 used 登记的是 ts——存量零迁移。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "一段有画面的生活片段"}])
    assert engine.build_bysource("10001", _NOW)
    assert engine._store.get_kv_str("bysource:used:10001") == "2026-09-22T10:00:00"


def test_mixed_window_dedup():
    """新旧条目混存窗口：各自登记各自键，去复用对两者都生效。"""
    engine = _make_engine(
        [
            {"ts": "2026-09-22T10:00:00", "text": "旧片段"},
            {
                "ts": "2026-09-22T11:00:00",
                "text": "新片段",
                "event_id": "ev_20260922110000_efef5678",
                "kind": "fragment",
            },
        ]
    )
    first = engine.build_bysource("10001", _NOW)
    second = engine.build_bysource("10001", _NOW)
    assert first != second, "两条都该被取到且不重复"
    assert engine.build_bysource("10001", _NOW) == "", "都用完后应无由头"


# ===== 批 2（v0.3.0 / R40）：seed 取材接线 + R44 签发冷却 =====


def test_seed_entry_sourced_with_seed_prefix():
    """seed 条目进候选（与 fragment 同闸同权），由头前缀区分素材类型。"""
    engine = _make_engine(
        [
            {
                "ts": "2026-10-07T10:00:00",
                "text": "巷口面馆的老板娘进了新米",
                "event_id": "ev_20261007100000_seed0001",
                "kind": "seed",
                "importance": "mid",
                "urgency": "short",
            }
        ]
    )
    detail = engine.build_bysource_detail("10001", _NOW)
    assert detail is not None
    assert "外面发生的一件事：巷口面馆的老板娘进了新米" == detail["text"]
    assert detail["origin"] == "seed", "origin=seed 供回执 scope 分析（批 0 裁定：可比 fragment vs seed 接住率）"
    assert detail["event_id"] == "ev_20261007100000_seed0001"


def test_seed_dedup_via_event_id():
    """seed 条目签发后按 event_id 去复用（同 fragment 语义）。"""
    engine = _make_engine(
        [
            {
                "ts": "2026-10-07T10:00:00",
                "text": "巷口的猫生了小猫",
                "event_id": "ev_20261007100000_seed0002",
                "kind": "seed",
            }
        ]
    )
    assert engine.build_bysource("10001", _NOW)
    assert "ev_20261007100000_seed0002" in engine._store.get_kv_str("bysource:used:10001")
    assert engine.build_bysource("10001", _NOW) == ""


def _make_cooldown_engine(*, fragment, sign_cooldown_hours=None):
    """带可选 [proactive].sign_cooldown_hours 的 engine（P4 冷却测试用）。

    单 engine 多用户：冷却 kv 挂 store（按 event_key 命名空间、与用户无关），
    与生产同一 store 共享语义一致。
    """
    engine = _make_engine([fragment])
    if sign_cooldown_hours is not None:
        engine._plugin.config.proactive = SimpleNamespace(sign_cooldown_hours=sign_cooldown_hours)
    return engine


def test_sign_cooldown_blocks_second_user_within_window():
    """R44：同一事件 T 小时内只签 1 人——用户甲签发后，乙在窗内取不到。"""
    fragment = {"ts": "2026-09-22T10:00:00", "text": "一段有画面的生活片段"}
    engine = _make_cooldown_engine(fragment=fragment)
    assert engine.build_bysource("10001", _NOW)

    assert engine.build_bysource("10002", _NOW + datetime.timedelta(hours=1)) == "", (
        "签发冷却窗内，同一事件不得签给第二人"
    )


def test_sign_cooldown_expired_allows():
    """冷却窗过后，乙可取到该事件（冷却只限时间窗，不是隔离）。"""
    fragment = {"ts": "2026-09-22T10:00:00", "text": "一段有画面的生活片段"}
    engine = _make_cooldown_engine(fragment=fragment)
    assert engine.build_bysource("10001", _NOW)

    assert "一段有画面的生活片段" in engine.build_bysource(
        "10002", _NOW + datetime.timedelta(hours=7)
    ), "默认 6h 窗过后应放行"


def test_sign_cooldown_respects_config():
    """冷却时长走 [proactive].sign_cooldown_hours 配置（无数据不写死常量）。"""
    fragment = {"ts": "2026-09-22T10:00:00", "text": "一段有画面的生活片段"}
    engine = _make_cooldown_engine(fragment=fragment, sign_cooldown_hours=1)
    assert engine.build_bysource("10001", _NOW)

    assert engine.build_bysource("10002", _NOW + datetime.timedelta(minutes=30)) == ""
    assert "一段有画面的生活片段" in engine.build_bysource("10002", _NOW + datetime.timedelta(hours=2))


def test_sign_cooldown_skips_keyless_candidates():
    """无 event_key 的候选（无 ts 无 event_id）不参与冷却（与 used 去重同一防御语义）。"""
    fragment = {"text": "没有时间戳的老条目"}
    engine = _make_cooldown_engine(fragment=fragment)
    assert engine.build_bysource("10001", _NOW)

    assert engine.build_bysource("10002", _NOW + datetime.timedelta(minutes=1)) != ""


def test_r44_comment_ruling_present():
    """红线⑤双处明文之一：sourcing 源码必须含「不构成生活线分叉」裁定原文。"""
    import inspect

    source = inspect.getsource(_SOURCING)
    assert "不构成生活线分叉" in source, "P4 代码注释裁定缺失（登记表 R44 同款明文要求）"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
