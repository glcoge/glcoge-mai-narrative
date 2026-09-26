"""state 结构级版本化（R12）+ state 重置语义测试（v0.2.0 批 2 · C1）。

锁定 ADR-0002 §7「存量数据处置：state 重置、chronicle 保留」：

- **state 结构带版本号**（复用 ``meta.version``，1 → 2），且该版本号**真的被读取**
  ——此前它写进 state 却从未被任何代码读取（批 2 前挖出的 F1）
- 开库遇到**旧版本 state** → 打 WARN + **原地重置**（不写迁移器，不为死格式陪葬），
  但**编年史必须保留**（它是晋升机证据源）
- ``/narrative reset`` 走的是 ``delete_keys_with_prefix("")`` 全清 kv —— 批 2 前
  这会把 schema_version 一起抹掉，下次开库误判成「旧库」再重置一遍（F2）。
  本测试锁「reset 之后 schema_version 仍在」

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_state_schema_version.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_state_schema_version.py
"""

from __future__ import annotations

import tempfile
import types
from pathlib import Path

import _synth_loader

_ENGINE = _synth_loader.load("services.state.engine")

STATE_SCHEMA_VERSION = _ENGINE.STATE_SCHEMA_VERSION
default_self_state = _ENGINE.default_self_state
default_branch_state = _ENGINE.default_branch_state
NarrativeEngine = _ENGINE.NarrativeEngine


def _make_engine(data_dir: Path) -> NarrativeEngine:
    """构造只依赖 store + logger 的最小引擎假件。"""
    plugin = types.SimpleNamespace(
        _store=_synth_loader.load("services.store").NarrativeStore(data_dir),
        config=types.SimpleNamespace(
            narrative=_synth_loader.sleep_config(timezone_offset_hours=8),
            plugin=types.SimpleNamespace(enabled=True),
        ),
        ctx=types.SimpleNamespace(logger=_synth_loader.null_logger()),
    )
    return NarrativeEngine(plugin)


# ─── 默认 state 带版本号 ────────────────────────────────────────


def test_default_self_state_carries_schema_version():
    """自我层默认 state 必须带当前 schema 版本。"""
    state = default_self_state()
    assert state["meta"]["version"] == STATE_SCHEMA_VERSION


def test_default_branch_state_carries_schema_version():
    """支线层默认 state 必须带当前 schema 版本。"""
    state = default_branch_state()
    assert state["meta"]["version"] == STATE_SCHEMA_VERSION


def test_schema_version_is_2_after_batch_2():
    """批 2 是本轮首次结构变更 → 版本从 1 提到 2（防有人忘了改常量）。"""
    assert STATE_SCHEMA_VERSION == 2


def test_default_state_has_no_dead_fields():
    """批 2 处决后默认 state 不再出现死字段（C2 的锚点，提前锁死形状）。"""
    self_inner = default_self_state()["state"]
    branch = default_branch_state()
    assert "habits" not in self_inner
    assert "hot_thread" not in self_inner.get("focus", {})
    assert "shared_secrets" not in branch["identity"]
    assert "user_notes" not in branch["state"]


# ─── 旧版本 state → WARN + 原地重置 ─────────────────────────────


def test_legacy_self_state_is_reset_with_warning():
    """旧版本（version=1）自我层 state → 重置为默认，且日志有 WARN。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        legacy = default_self_state()
        legacy["meta"]["version"] = 1
        legacy["state"]["mood"]["label"] = "旧版本遗留心情"
        legacy["state"]["habits"] = ["旧字段"]
        engine._store.set_kv("self", legacy)

        warnings = []
        engine._plugin.ctx.logger = types.SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: warnings.append(a[0] if a else ""),
            error=lambda *a, **k: None,
        )

        state = engine.load_self_state()
        assert state["meta"]["version"] == STATE_SCHEMA_VERSION
        assert state["state"]["mood"]["label"] != "旧版本遗留心情"
        assert "habits" not in state["state"]
        assert warnings, "旧版本 state 重置必须打 WARN（静默重置会让人困惑）"


def test_legacy_branch_state_is_reset():
    """旧版本支线层 state 同样重置。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        legacy = default_branch_state()
        legacy["meta"]["version"] = 1
        legacy["state"]["familiarity"] = 99.0
        engine._store.set_kv("branch:10001", legacy)

        state = engine.load_branch_state("10001")
        assert state["meta"]["version"] == STATE_SCHEMA_VERSION
        # 重置后 first_met 重新登记（原先的旧值不作数）
        assert state["relationship"]["first_met"] != ""


def test_current_version_state_is_kept():
    """版本已是当前的 state 必须**原样保留**（不能每次开库都重置）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        state = default_self_state()
        state["state"]["mood"]["label"] = "吃过了"
        engine.save_self_state(state)

        reloaded = engine.load_self_state()
        assert reloaded["state"]["mood"]["label"] == "吃过了"


def test_reset_preserves_chronicle():
    """重置 state 不得动编年史（晋升机证据源，ADR-0002 §7）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        engine._store.append_chronicle("self", "daily", "2026-09-26", "今天聊了很多。")
        engine.load_self_state()

        engine.reset_state()

        assert engine._store.count_chronicle("self") == 1


def test_reset_keeps_schema_version():
    """reset 后重新开库不得把当前 state 误判成旧库再重置一遍（F2）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = _make_engine(Path(tmp))
        engine.load_self_state()
        engine.reset_state()

        # 重新构造一个引擎（模拟重启后再开库）
        engine2 = _make_engine(Path(tmp))
        warnings = []
        engine2._plugin.ctx.logger = types.SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: warnings.append(a[0] if a else ""),
            error=lambda *a, **k: None,
        )
        state = engine2.load_self_state()
        assert state["meta"]["version"] == STATE_SCHEMA_VERSION
        assert not warnings, "reset 只清运行态，不应让下次开库误判旧库"


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
