"""依赖束（深化 B / grilling Q1a）：装配完整性、可选缺省宽容、冻结、懒组装、重绑定。

B0 是**纯机制批**（零消费方迁移，B1~B3 才改签名）：本文件的断言就是 ``Deps``
契约的全部守卫。最要紧的一条是**旧桩兼容通道**——B1~B3 的签名迁移依赖
「旧式 ``__new__`` 桩缺 ``_deps`` 时 ``engine.deps`` 仍可用」：迁移期测试桩
一律零改动，避免用装配噪音淹没真回归。

热重载语义另有一条硬事实要锁（易踩坑）：宿主每次热重载都会**换新配置实例**
（SDK ``set_plugin_config`` 重新 validate；runner ``_handle_config_updated``
先注入新配置、后回调 ``on_config_update``）。故 engine 快照不会自动跟随，
必须在回调里 ``rebind_deps()``——接线由本文件的 AST 守卫锁死。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_deps.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_deps.py
"""

from __future__ import annotations

import ast
import datetime
import types
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import _synth_loader

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

_ENGINE = _synth_loader.load("services.state.engine")
NarrativeEngine = _ENGINE.NarrativeEngine
#: engine 实例的 creator 由 engine 模块**自己的 import 面**构造（`load` 每次调用
#: 都重新执行模块：另起一次 load("services.creation.creator") 或再 load 一次
#: engine 都会拿到**另一个类对象**，isinstance 假红）——故取同一次加载的导入面。
CreatorClient = _ENGINE.CreatorClient

_NOW = datetime.datetime(2026, 10, 8, 12, 0, 0)


async def _native_config_get(key, default=None):
    """主程序配置读口的异步替身（ctx.config.get 的形状）。"""
    return default


def _fake_plugin() -> types.SimpleNamespace:
    """生产形状的假 plugin：字段面与 ``plugin.py.__init__`` 的初始面一致。"""
    return types.SimpleNamespace(
        config=types.SimpleNamespace(narrative=types.SimpleNamespace(timezone_offset_hours=8)),
        ctx=types.SimpleNamespace(
            logger=_synth_loader.make_logger(),
            config=types.SimpleNamespace(get=_native_config_get),
        ),
        _store=types.SimpleNamespace(name="store"),
        _telemetry=types.SimpleNamespace(name="telemetry"),
        _lorebook=types.SimpleNamespace(name="lorebook"),
        _streams=types.SimpleNamespace(name="streams"),
        _group_streams=types.SimpleNamespace(name="group-streams"),
    )


def _stub_engine() -> NarrativeEngine:
    """旧式 ``__new__`` 桩：只手挂 ``_plugin``（config+ctx）与 ``_store``。

    形状取自 ``test_fragment_pending_capacity`` 的 ``_make_engine``——
    ``_plugin`` **不带** ``_store``，故本形状同时锁「装配读 ``self._store`` 而非
    ``plugin._store``」这条实现约定。
    """
    stub = NarrativeEngine.__new__(NarrativeEngine)
    stub._plugin = types.SimpleNamespace(
        config=types.SimpleNamespace(narrative=types.SimpleNamespace(timezone_offset_hours=8)),
        ctx=types.SimpleNamespace(logger=_synth_loader.make_logger()),
    )
    stub._store = types.SimpleNamespace(name="stub-store")
    return stub


# ─── 装配合集 ───────────────────────────────────────────────────


def test_construction_assembly_covers_all_fields():
    """真实实例构造期装配：11 个字段逐一归位（含 local_now 可调用）。"""
    plugin = _fake_plugin()
    engine = NarrativeEngine(plugin)

    deps = engine.deps
    assert deps.config is plugin.config
    assert deps.store is plugin._store
    assert deps.logger is plugin.ctx.logger
    assert deps.state is engine
    assert isinstance(deps.creator, CreatorClient)
    assert deps.telemetry is plugin._telemetry
    assert deps.lorebook is plugin._lorebook
    assert deps.streams is plugin._streams
    assert deps.group_streams is plugin._group_streams
    assert deps.native_config_get == plugin.ctx.config.get
    assert isinstance(deps.local_now(), datetime.datetime)


def test_optional_subsystems_tolerate_missing_fields():
    """旧桩缺可选字段（_telemetry/_lorebook/_streams/_group_streams/ctx.config/_creator）
    → 全 None、不 AttributeError（≡ 现行 getattr 宽容语义）。"""
    deps = _stub_engine().deps

    assert deps.telemetry is None
    assert deps.lorebook is None
    assert deps.streams is None
    assert deps.group_streams is None
    assert deps.native_config_get is None
    assert deps.creator is None
    assert deps.state is not None  # state 恒为桩自身


# ─── 懒组装兼容通道（B1~B3 旧桩零改动的依据） ──────────────────


def test_lazy_assembly_channel_serves_legacy_stubs():
    """旧桩 ``engine.deps`` 现场组装：读手挂 ``_plugin``/``_store``，且**不缓存**。

    不缓存是刻意的契约（见 ``deps`` 属性 docstring）：旧桩常以「换 config 再调
    一次」驱动用例，缓存会让第二次调用看到陈旧快照。
    """
    stub = _stub_engine()
    config = stub._plugin.config

    deps = stub.deps
    assert deps.config is config
    assert deps.store is stub._store
    assert deps.logger is stub._plugin.ctx.logger
    assert stub._deps is None, "懒组装不得回写 _deps（旧桩保持零副作用）"

    new_config = types.SimpleNamespace(narrative=types.SimpleNamespace(timezone_offset_hours=9))
    stub._plugin.config = new_config
    assert stub.deps.config is new_config, "懒组装不缓存：换 config 后必须立刻可见"


# ─── 冻结快照 ───────────────────────────────────────────────────


def test_deps_is_frozen():
    """冻结 dataclass：字段不可再赋值（防装配后被就地改坏）。"""
    deps = NarrativeEngine(_fake_plugin()).deps
    with pytest.raises(FrozenInstanceError):
        deps.config = None


# ─── 热重载重绑定 ───────────────────────────────────────────────


def test_rebind_follows_new_config_instance():
    """换新配置实例：快照不自动跟随；``rebind_deps`` 后跟新，稳定子系统身份不变。"""
    plugin = _fake_plugin()
    engine = NarrativeEngine(plugin)
    old_config = plugin.config

    new_config = types.SimpleNamespace(narrative=types.SimpleNamespace(timezone_offset_hours=9))
    plugin.config = new_config

    assert engine.deps.config is old_config, "装配是显式的：快照不自动跟随"
    engine.rebind_deps()
    assert engine.deps.config is new_config
    assert engine.deps.store is plugin._store, "稳定子系统（store）身份不因重绑定变化"


def test_rebind_rebuilds_local_now_closure():
    """``local_now`` 闭包读**快照** config 的时区偏移；重绑定后跟新偏移。

    用 monkeypatch 钉住 ``engine`` 模块的 ``local_now``（系统时区差异会让真实现
    在 UTC 环境对任何偏移都返回墙钟，无法区分 8 与 3）。
    """
    engine_globals = NarrativeEngine._assemble_deps.__globals__
    calls: list = []
    original = engine_globals["local_now"]
    engine_globals["local_now"] = lambda offset: calls.append(offset) or _NOW
    try:
        plugin = _fake_plugin()
        engine = NarrativeEngine(plugin)
        assert engine.deps.local_now() is _NOW
        plugin.config = types.SimpleNamespace(
            narrative=types.SimpleNamespace(timezone_offset_hours=3)
        )
        engine.rebind_deps()
        assert engine.deps.local_now() is _NOW
    finally:
        engine_globals["local_now"] = original

    assert calls == [8, 3], "闭包必须读快照 config 的时区偏移，重绑定后跟新"


# ─── 接线守卫（AST） ────────────────────────────────────────────


def test_plugin_wires_rebind_at_load_and_config_update():
    """``on_load`` 尾部与 ``on_config_update`` 都必须调用 ``rebind_deps``。

    前者的必要性：engine 构造时 ``_streams`` / ``_lorebook`` 尚未创建，快照要靠
    加载尾部补全（且必须在 ``_reconcile_all`` 前——tick 一启动就读 deps）；
    后者的必要性：宿主换新配置实例后不重绑定 = 永远读旧配置。
    """
    tree = ast.parse((PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8"))
    methods = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.setdefault(node.name, node)

    for method_name in ("on_load", "on_config_update"):
        node = methods.get(method_name)
        assert node is not None, f"未找到 {method_name}"
        calls = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "rebind_deps"
        ]
        assert calls, f"{method_name} 未调用 rebind_deps"


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))