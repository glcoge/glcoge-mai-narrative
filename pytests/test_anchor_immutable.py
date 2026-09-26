"""锚定层不可写测试（v0.2.0 批 2 · C5，ADR-0002 §9）。

锚定层＝宿主 ``[personality]`` + 插件世界观/规则，**永不改变**。
本测试把这条承诺变成可执行约束：

- ``assert_writable`` 对锚定层字段（含 ``identity.`` / ``personality`` /
  ``character.`` 整棵子树）抛 ``PermissionError``
- 慢变/漂移字段可写
- 锚定层真源是**配置**，不在运行时 state 内——state 里出现锚定字段即为事故
- 批 3/4 的写入路径必须调用同一个守卫（本测试现在就锁死，届时自然覆盖）

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_anchor_immutable.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_anchor_immutable.py
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

import _synth_loader

_CONTINUITY = _synth_loader.load("services.state.continuity")
_ENGINE = _synth_loader.load("services.state.engine")

assert_writable = _CONTINUITY.assert_writable
is_anchor_field = _CONTINUITY.is_anchor_field
default_self_state = _ENGINE.default_self_state
default_branch_state = _ENGINE.default_branch_state
NarrativeEngine = _ENGINE.NarrativeEngine

#: 锚定层字段样本（含整棵子树的代表）
_ANCHOR_PATHS = [
    "identity.world",
    "identity.values",
    "identity.world_rules",
    "identity.world_rules.0",
    "personality",
    "personality.behavior_style",
    "character.traits",
]

#: 非锚定（可写）字段样本
_WRITABLE_PATHS = [
    "relationship.trust",
    "relationship.closeness",
    "relationship.stage",
    "perspective.world_view",
    "perspective.life_goals",
    "state.mood.energy",
]


@pytest.mark.parametrize("path", _ANCHOR_PATHS)
def test_anchor_path_is_not_writable(path):
    """锚定层字段写入必须被拒（抛 PermissionError）。"""
    with pytest.raises(PermissionError):
        assert_writable(path)


@pytest.mark.parametrize("path", _WRITABLE_PATHS)
def test_non_anchor_path_is_writable(path):
    """慢变/漂移字段可写，且原样返回路径（便于链式下标赋值）。"""
    assert assert_writable(path) == path


def test_assert_writable_rejects_empty_prefix_safely():
    """空路径不误判为锚定（is_anchor_field 对空串返回 False）。"""
    assert not is_anchor_field("")
    assert assert_writable("") == ""


def test_assert_writable_does_not_false_positive_on_similar_prefix():
    """前缀匹配不得误伤：``identityric`` 不是 ``identity.`` 子树。"""
    assert assert_writable("identityric") == "identityric"


# ─── 运行时 state 不得承载锚定字段 ──────────────────────────────


def test_self_state_has_no_anchor_namespace():
    """自我层 state 里不得出现锚定字段（真源是插件 config [identity]）。"""
    inner = default_self_state()["state"]
    for key in inner:
        assert not is_anchor_field(key), f"self state 出现锚定字段: {key}"
    assert "identity" not in inner


def test_branch_state_has_no_anchor_namespace():
    """支线层 state 亦不得承载世界规则/价值观（只留关系与计数）。"""
    branch = default_branch_state()
    assert "world" not in branch.get("identity", {})
    assert "values" not in branch.get("identity", {})
    assert "world_rules" not in branch.get("identity", {})


def test_anchor_source_is_config_not_state():
    """锚定层读取唯一入口是 config.identity —— 引擎不得从 state 读锚定。"""
    with tempfile_dir() as tmp:
        plugin = types.SimpleNamespace(
            _store=_synth_loader.load("services.store").NarrativeStore(tmp),
            config=types.SimpleNamespace(
                identity=types.SimpleNamespace(
                    world="测试世界", values=["诚实"], world_rules=["没有魔法"]
                ),
                narrative=_synth_loader.sleep_config(timezone_offset_hours=8),
                plugin=types.SimpleNamespace(enabled=True),
            ),
            ctx=types.SimpleNamespace(logger=_synth_loader.null_logger()),
        )
        engine = NarrativeEngine(plugin)
        state = engine.load_self_state()
        # 锚定内容不在 state 里，只在 config 里可读
        assert "测试世界" not in str(state)
        assert plugin.config.identity.world == "测试世界"


class tempfile_dir:
    """极简临时目录上下文（避免重复 import tempfile 的样板）。"""

    def __enter__(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name)

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
