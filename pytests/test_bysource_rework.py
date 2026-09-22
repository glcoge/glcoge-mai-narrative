"""由头返工 P1 测试（2026-09-22，issue-bysource-rework）。

覆盖三项：
- **B 去复用**：已用作由头的片段不再二次使用（kv `bysource:used:{user_id}`，按用户隔离）
- **C 质量门槛**：minor 档（无素材的纯状态切片）不单独作由头 → 宁可跳过本轮
- **G 迟来承接**：24 小时内回复记为 `check_late_reply`，每条只计一次

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
    sched._sent_at = {}
    sched._sent_long = {}
    sched._pending_at = {}
    return sched


def test_late_reply_within_24h():
    """5 小时后回复：30 分钟口径不算，24 小时口径算一次。"""
    sched = _make_scheduler()
    sent_at = _NOW
    sched._sent_long["10001"] = [sent_at]

    replied_at = sent_at + datetime.timedelta(hours=5)
    assert sched.check_late_reply("10001", replied_at) is True
    assert sched.check_late_reply("10001", replied_at) is False, "同一条只计一次"


def test_no_late_reply_after_24h():
    sched = _make_scheduler()
    sent_at = _NOW
    sched._sent_long["10001"] = [sent_at]
    assert sched.check_late_reply("10001", sent_at + datetime.timedelta(hours=25)) is False


def test_late_tracks_record_sent():
    """触发主动开口时，24h 记录与 30min 记录同时登记。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头")
    assert sched._sent_at.get("10001")
    assert sched._sent_long.get("10001"), "迟来承接需要 24h 记录"


def test_replied_in_30min_not_counted_twice():
    """已被 30min 口径记为"被接住"的消息，不得再被 24h 口径重复计数。

    plugin.py 的判定链是 check_reply → elif check_late_reply；若 30min 命中时不从
    _sent_long 弹出对应条目，用户下一条 inbound 会把同一条主动消息再记一次
    proactive_replied_24h，指标系统性偏高、无法与 A2 基线（30min 口径 24%）对照。
    """
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头")
    replied_at = _NOW + datetime.timedelta(minutes=5)

    assert sched.check_reply("10001", replied_at) is True, "30min 口径应命中"
    assert sched.check_late_reply("10001", replied_at + datetime.timedelta(hours=2)) is False, (
        "同一条消息不应既算 30min 接住、又算 24h 迟来承接"
    )


def test_30min_ack_pops_only_one_entry():
    """多条主动消息时，一次 30min 承接只抵消**最新**一条，其余仍在 24h 窗口内有效。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "stream-1", _NOW, "由头甲")
    sched.record_sent("10001", "stream-1", _NOW + datetime.timedelta(minutes=10), "由头乙")
    assert len(sched._sent_long["10001"]) == 2

    assert sched.check_reply("10001", _NOW + datetime.timedelta(minutes=12)) is True
    assert len(sched._sent_long["10001"]) == 1, "一次承接只应抵消一条记录"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
