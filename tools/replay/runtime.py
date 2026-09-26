"""回放台运行入口：把插件模块挂到合成包下加载，绕开 maibot_sdk 依赖。

复用 `pytests/_synth_loader` 的既有机制（单点维护），不另起一套。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PYTESTS_DIR = PLUGIN_ROOT / "pytests"


def _import_synth_loader() -> Any:
    if str(PYTESTS_DIR) not in sys.path:
        sys.path.insert(0, str(PYTESTS_DIR))
    import _synth_loader  # noqa: PLC0415

    return _synth_loader


def load(rel_name: str) -> Any:
    """按相对名加载插件模块（如 ``services.render.planner_block``）。"""
    return _import_synth_loader().load(rel_name)


def load_render() -> Any:
    """加载注入渲染模块（批 1 起改为 services.render.planner_block）。"""
    return load("services.render.planner_block")


def fixtures() -> Any:
    """测试夹具模块（``pytests/_synth_loader``）。

    回放台复用它的 config 夹具（``promotion_config`` / ``sleep_config`` 等），
    **不另起第二份默认值**——两份默认值迟早漂移，那时回放台会开始验证一个
    与插件实际不同的世界。
    """
    return _import_synth_loader()
