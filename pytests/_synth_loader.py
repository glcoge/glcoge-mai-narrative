"""narrative 测试共享合成包 loader（四份测试文件此前各自内嵌同一套实现）。

插件目录名含连字符（``glcoge-mai-narrative``）不能直接作为包名 import——
借鉴 mai-diary 的做法，用 ``importlib`` 挂到合成包名下加载。此前
test_plugin_hooks / test_engine_rules / test_render_turn / test_creator_route
各自复制同一套 loader 与独立运行入口（重复约 120 行），2026-09-13 体检（C3）
收敛到此单点：loader 需要修时只改这一处。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SYNTH_PKG = "_narrative_test_plugin"


def _install_synth_package() -> None:
    """注册合成包（根 + services），使插件模块的相对导入可解析。"""
    if _SYNTH_PKG in sys.modules:
        return
    root = types.ModuleType(_SYNTH_PKG)
    root.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    sys.modules[_SYNTH_PKG] = root
    services = types.ModuleType(f"{_SYNTH_PKG}.services")
    services.__path__ = [str(PLUGIN_ROOT / "services")]  # type: ignore[attr-defined]
    sys.modules[f"{_SYNTH_PKG}.services"] = services


def load(rel_name: str) -> types.ModuleType:
    """按相对名加载插件模块（如 ``"services.engine"`` / ``"plugin"``）。

    命中目录时回退加载其 ``__init__.py``（如 ``"services"`` → services/__init__.py，
    真正执行再导出，plugin.py 依赖它）。
    """
    _install_synth_package()
    file_path = PLUGIN_ROOT.joinpath(*rel_name.split("."))
    if file_path.is_dir():
        file_path = file_path / "__init__.py"
    else:
        file_path = file_path.with_suffix(".py")
    full_name = f"{_SYNTH_PKG}.{rel_name}"
    spec = importlib.util.spec_from_file_location(full_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {full_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def null_logger() -> types.SimpleNamespace:
    """静默 logger（v0.1.10）：睡眠状态机在入睡/醒来/被吵醒时会打 INFO，
    测试里的假 plugin 必须提供 ctx.logger，否则转换瞬间 AttributeError。
    """
    return types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )


# [narrative] 睡眠态字段的出厂值（与 config.py 的 Field default 保持一致）。
# 单个用例要改某项时传关键字覆盖；要关闭睡眠态传 sleep_time=""。
SLEEP_DEFAULTS = {
    "sleep_time": "23:30",
    "wake_time": "07:00",
    "sleep_delay_max_minutes": 60,
    "sleep_delay_recent_minutes": 10,
    "woken_awake_minutes": 30,
    "energy_woken_penalty": 0.08,
    "energy_woken_floor": 0.3,
    "wake_fragment_enabled": True,
    "sleep_pre_sleep_hint_minutes": 25,
}


def sleep_config(**overrides) -> types.SimpleNamespace:
    """睡眠态配置夹具：默认全开，传 ``sleep_time=""`` 即关闭整个睡眠态。"""
    merged = dict(SLEEP_DEFAULTS)
    merged.update(overrides)
    return types.SimpleNamespace(**merged)


def run_standalone(globals_dict: dict) -> int:
    """独立运行入口：执行当前测试模块全部 test_ 函数并打印结果。

    供各测试文件 ``if __name__ == "__main__":`` 一行调用，保持
    pytest 之外的备用运行方式（返回码 0=全过，1=有失败）。
    """
    fns = [
        (name, obj)
        for name, obj in list(globals_dict.items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    return 0 if not failed else 1
