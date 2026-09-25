"""由头取材（sourcing）测试：批 1 拆分 + 撞车判定 + 命令兜底。

覆盖：
- B6 纯移动：``build_bysource`` / ``compute_share_urge`` / ``record_urge_feedback``
  迁到 ``proactive/sourcing.py`` 后，engine 的对外 API 与行为不变
- B7 取材优先级：tier 只定详略，真正的取材顺序＝**未用过 + 与最近对话不撞车**（R23）
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
_SOURCING = _synth_loader.load("services.proactive.sourcing")
_MESSAGE = _synth_loader.load("services.message")

NarrativeEngine = _ENGINE_MOD.NarrativeEngine
overlap_ratio = _SOURCING.overlap_ratio
build_bysource = _SOURCING.build_bysource
looks_like_command = _MESSAGE.looks_like_command

_NOW = datetime.datetime(2026, 9, 22, 11, 37, 0)
_UID = "10001"


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
        narrative=SimpleNamespace(enabled=True, mode_user_ids=[_UID], energy_baseline=0.55),
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
            "focus": {"hot_thread": "", "pending_events": pending},
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
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "去海边走了走", "tier": "normal"}])
    assert "去海边走了走" in engine.build_bysource(_UID, _NOW)
    assert engine.compute_share_urge(_UID) > 0
    engine.record_urge_feedback(_UID, "caught")
    assert engine._self_state["state"]["urge"] == 0.6


def test_sourcing_functions_callable_directly():
    """迁出后可脱离 engine 类直接调用（新模块的对外契约）。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "去海边走了走", "tier": "normal"}])
    assert "去海边走了走" in build_bysource(engine, _UID, _NOW)
    assert _SOURCING.compute_share_urge(engine, _UID) > 0


# ===== B7：取材优先级 —— 与最近对话不撞车（R23） =====


def test_overlap_ratio_sanity():
    assert overlap_ratio("", "") == 0.0
    assert overlap_ratio("去海边走了走", "去海边走了走") == 1.0
    assert overlap_ratio("去海边走了走", "今天写完了作业") < 0.2


def test_bysource_skips_fragment_overlapping_recent_dialogue():
    """由头要是"新事"：与最近对话高度撞车的片段不作由头（宁可跳过）。"""
    same = "去海边走了走，风很大"
    engine = _make_engine(
        [{"ts": "2026-09-22T10:00:00", "text": same, "tier": "normal"}],
        events=[{"bysource": same}],
    )
    assert engine.build_bysource(_UID, _NOW) == "", "撞车片段应被跳过（本轮主动取消）"


def test_bysource_keeps_non_overlapping_fragment():
    """不撞车时照常取材（过滤不能把正常功能滤掉）。"""
    engine = _make_engine(
        [{"ts": "2026-09-22T10:00:00", "text": "去海边走了走，风很大", "tier": "normal"}],
        events=[{"bysource": "今天写完了作业"}],
    )
    assert "去海边走了走" in engine.build_bysource(_UID, _NOW)


def test_minor_tier_still_not_used_alone():
    """minor 档质量门槛保留（tier 不参与优先级，但仍拦太薄的内容）。"""
    engine = _make_engine([{"ts": "2026-09-22T10:00:00", "text": "无素材切片", "tier": "minor"}])
    assert engine.build_bysource(_UID, _NOW) == ""


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
