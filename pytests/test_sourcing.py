"""由头取材（sourcing）测试：批 1 拆分 + 撞车判定 + 命令兜底。

覆盖：
- B6 纯移动：``build_bysource`` / ``compute_share_urge`` / ``record_urge_feedback``
  迁到 ``proactive/sourcing.py`` 后，engine 的对外 API 与行为不变
- B7 取材优先级：取材顺序＝**未用过 + 与最近对话不撞车**（R23；原 tier 三信号 2026-09-30 已随功能删除）
- B8 命令兜底：``looks_like_command`` 本地正则（R9，宿主修复后可删）

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_sourcing.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_sourcing.py
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_synth_loader.load("services.state.engine")  # 触发 services/__init__.py
_ENGINE_MOD = _synth_loader.load("services.state.engine")
sourcing = _synth_loader.load("services.proactive.sourcing")
_SOURCING = _synth_loader.load("services.proactive.sourcing")
_MESSAGE = _synth_loader.load("services.message")

NarrativeEngine = _ENGINE_MOD.NarrativeEngine
overlap_ratio = _SOURCING.overlap_ratio
build_bysource = _SOURCING.build_bysource
looks_like_command = _MESSAGE.looks_like_command

_NOW = datetime.datetime(2026, 9, 22, 11, 37, 0)
_UID = "10001"


_Logger = _synth_loader.null_logger


class _FakeStore:
    def __init__(self, events=None):
        self.kv_str: dict = {}
        self.events = list(events or [])

    def list_events(self, scope, limit=20):
        return self.events[:limit]

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value


def _make_engine(pending, events=None):
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True, mode_user_ids=[_UID], energy_baseline=0.55, fragment_pending_max=12
        ),
        proactive=SimpleNamespace(
            urge_enabled=True,
            urge_base=0.5,
            urge_gain=0.1,
            urge_decay=0.25,
            urge_branch_floor=0.3,
        ),
    )
    engine = NarrativeEngine.__new__(NarrativeEngine)
    engine._plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=_Logger()))
    engine._store = _FakeStore(events)
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6, "last_shift_ts": ""},
            "routine": {"phase": "午后"},
            "focus": {"pending_events": pending},
            "urge": 0.5,
        }
    }
    branch = {"state": {"familiarity": 10.0, "milestones": [], "urge_factor": 1.0},
              "identity": {"stage": "陌生人"}}
    engine.load_branch_state = lambda uid: branch
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    engine.save_branch_state = lambda uid, b: None
    return engine


# ===== B6：纯移动后对外 API 不变 =====


def test_engine_methods_still_work_after_move():
    """engine.build_bysource / compute_share_urge / record_urge_feedback 行为不变。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "去海边走了走"}])
    assert "去海边走了走" in sourcing.build_bysource(engine.deps,_UID, _NOW)
    assert sourcing.compute_share_urge(engine.deps,_UID) > 0
    sourcing.record_urge_feedback(engine.deps,_UID, "caught")
    assert engine._self_state["state"]["urge"] == 0.6


def test_sourcing_functions_callable_directly():
    """迁出后可脱离 engine 类直接调用（新模块的对外契约）。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "去海边走了走"}])
    assert "去海边走了走" in build_bysource(engine.deps, _UID, _NOW)
    assert _SOURCING.compute_share_urge(engine.deps, _UID) > 0


# ===== B7：取材优先级 —— 与最近对话不撞车（R23） =====


def test_overlap_ratio_sanity():
    assert overlap_ratio("", "") == 0.0
    assert overlap_ratio("去海边走了走", "去海边走了走") == 1.0
    assert overlap_ratio("去海边走了走", "今天写完了作业") < 0.2


def test_bysource_skips_fragment_overlapping_recent_dialogue():
    """由头要是"新事"：与最近对话高度撞车的片段不作由头（宁可跳过）。"""
    same = "去海边走了走，风很大"
    engine = _make_engine(
        [{"ts": "2026-09-22T10:00:00", "text": same}],
        events=[{"bysource": same}],
    )
    assert sourcing.build_bysource(engine.deps,_UID, _NOW) == "", "撞车片段应被跳过（本轮主动取消）"


def test_bysource_keeps_non_overlapping_fragment():
    """不撞车时照常取材（过滤不能把正常功能滤掉）。"""
    engine = _make_engine(
        [{"ts": "2026-09-22T10:00:00", "text": "去海边走了走，风很大"}],
        events=[{"bysource": "今天写完了作业"}],
    )
    assert "去海边走了走" in sourcing.build_bysource(engine.deps,_UID, _NOW)


def test_full_window_sourcing_beyond_last_two():
    """全窗口取材（方案 §7 / P20，2026-09-30）：第 3 旧的片段同样可作由头。

    旧实现只取 ``pending[-2:]``——把最新两条预登记为已用后，候选必然为空，
    ``build_bysource`` 返回 ""（宁可跳过）。全窗口化后第 3 旧的片段重新可见。
    """
    pending = [
        {"ts": "2026-09-22T09:00:00", "text": "最旧的片段"},
        {"ts": "2026-09-22T10:00:00", "text": "次新的片段"},
        {"ts": "2026-09-22T11:00:00", "text": "最新的片段"},
    ]
    engine = _make_engine(pending)
    # 最新两条登记为已用 → 旧窗口（[-2:]）下无候选
    engine._store.set_kv_str(
        _SOURCING._bysource_used_key(_UID),
        "2026-09-22T10:00:00,2026-09-22T11:00:00",
    )
    chosen = sourcing.build_bysource(engine.deps,_UID, _NOW)
    assert "最旧的片段" in chosen, "全窗口下第 3 旧的片段应可作由头"


def test_used_registry_width_follows_pending_max():
    """去重登记表宽度派生自 fragment_pending_max（§15-4）：不再硬编码 8。"""
    engine = _make_engine([{"ts": "2026-09-22T11:00:00", "text": "片段甲"}])
    engine._plugin.config.narrative.fragment_pending_max = 3
    key = _SOURCING._bysource_used_key(_UID)
    # 预填 6 条更旧的登记（2026-01-01）→ 选中后再登记 1 条，共 7 条，只应留最新 3 条
    engine._store.set_kv_str(key, ",".join(f"2026-01-01T00:{i:02d}:00" for i in range(6)))
    assert "片段甲" in sourcing.build_bysource(engine.deps,_UID, _NOW)
    stored = engine._store.get_kv_str(key)
    assert len(stored.split(",")) == 3, "登记表宽度应跟随 fragment_pending_max=3"
    assert "2026-09-22T11:00:00" in stored, "最新选中者必须保留在登记表中"


def test_highlight_fragment_double_weight_slot():
    """高光片段 2 倍权重（Q12 / §4.2）：**占槽实现**——3 普通 + 1 高光 = 5 槽，高光占 2 槽。

    选择是确定性的（``candidates[seed % len(candidates)]``），故枚举 5 个连续小时覆盖
    ``seed % 5`` 的全部余数，统计选中高光的次数应为 2/5。每次新建 engine（选中的 ts
    会被登记进去重表，污染后续候选集）。
    """
    pending = [
        {"ts": "2026-09-22T09:00:00", "text": "普通甲", "highlight": False},
        {"ts": "2026-09-22T10:00:00", "text": "普通乙", "highlight": False},
        {"ts": "2026-09-22T11:00:00", "text": "普通丙", "highlight": False},
        {"ts": "2026-09-22T12:00:00", "text": "高光片段", "highlight": True},
    ]
    hits = 0
    for hour in range(5):
        engine = _make_engine([dict(item) for item in pending])
        now = datetime.datetime(2026, 9, 22, hour, 0, 0)
        if "高光片段" in sourcing.build_bysource(engine.deps,_UID, now):
            hits += 1
    assert hits == 2, f"高光应占 5 槽中的 2 槽（实测 {hits}/5）"


# ===== B8：命令消息本地正则兜底（R9） =====


def test_looks_like_command():
    assert looks_like_command("/narrative status")
    assert looks_like_command("  /help")
    assert looks_like_command("／帮助")
    assert not looks_like_command("今天天气不错")
    assert not looks_like_command("")
    assert not looks_like_command("a/b")


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
