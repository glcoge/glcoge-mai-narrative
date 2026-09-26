"""R14 启动期锚定一致性比对测试（v0.2.0 批 2 · C5）。

设计要点（E5 裁决）：
- **默认关**（``[anchor].consistency_check=false``）——启动期同步 LLM 调用一旦
  慢/失败会拖垮启动；默认关 = 通道建好、默认不跑
- 开启后：不一致仅 WARN，**不阻断加载**
- 任何异常（读配置失败 / LLM 失败）只降级为 debug，绝不让插件加载失败

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_anchor_consistency.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_anchor_consistency.py
"""

from __future__ import annotations

import asyncio
import types

import _synth_loader

_SYNTH_SERVICES = _synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin


class _Logger:
    def __init__(self):
        self.warnings = []
        self.infos = []
        self.debugs = []

    def debug(self, *a, **k):
        self.debugs.append(a[0] % a[1:] if len(a) > 1 else (a[0] if a else ""))

    def info(self, *a, **k):
        self.infos.append(a[0] % a[1:] if len(a) > 1 else (a[0] if a else ""))

    def warning(self, *a, **k):
        self.warnings.append(a[0] % a[1:] if len(a) > 1 else (a[0] if a else ""))

    def error(self, *a, **k):
        pass


def _make_plugin(
    *,
    consistency_check=False,
    world="测试世界",
    values=None,
    host_personality="一个普通的高中女生",
    judge=None,
    raise_on_get=False,
):
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    logger = _Logger()
    plugin._plugin_config_instance = types.SimpleNamespace(
        anchor=types.SimpleNamespace(consistency_check=consistency_check),
        identity=types.SimpleNamespace(
            world=world, values=["诚实"] if values is None else values
        ),
    )

    async def _get(key, default=""):
        if raise_on_get:
            raise RuntimeError("读配置炸了")
        if key == "personality.personality":
            return host_personality
        return default

    plugin._ctx = types.SimpleNamespace(
        logger=logger, config=types.SimpleNamespace(get=_get)
    )
    if judge is not None:
        plugin._judge_anchor_consistency = judge
    return plugin, logger


def test_disabled_by_default_skips_entirely():
    """默认关：不读宿主配置、不调 LLM。"""
    called = {"judge": 0}

    async def _judge(*a, **k):
        called["judge"] += 1
        return {"conflicts": "x"}

    plugin, logger = _make_plugin(consistency_check=False, judge=_judge)
    asyncio.run(plugin._check_anchor_consistency())
    assert called["judge"] == 0
    assert not logger.warnings
    assert not logger.infos


def test_enabled_and_consistent_logs_info():
    async def _judge(*a, **k):
        return {"conflicts": ""}

    plugin, logger = _make_plugin(consistency_check=True, judge=_judge)
    asyncio.run(plugin._check_anchor_consistency())
    assert any("通过" in line for line in logger.infos)
    assert not logger.warnings


def test_enabled_and_conflicting_logs_warning_not_blocking():
    """不一致仅 WARN（不抛异常 = 不阻断加载）。"""

    async def _judge(*a, **k):
        return {"conflicts": "宿主是大学生，插件世界是神兽"}

    plugin, logger = _make_plugin(consistency_check=True, judge=_judge)
    asyncio.run(plugin._check_anchor_consistency())  # 不得抛
    assert any("双人格冲突" in line for line in logger.warnings)


def test_empty_plugin_anchor_skips():
    """插件侧 world/values 全空 = 没配锚定，不是"不一致"→ 不比对。"""
    called = {"judge": 0}

    async def _judge(*a, **k):
        called["judge"] += 1
        return {"conflicts": ""}

    plugin, logger = _make_plugin(
        consistency_check=True, world="", values=[], judge=_judge
    )
    asyncio.run(plugin._check_anchor_consistency())
    assert called["judge"] == 0


def test_empty_host_personality_skips():
    called = {"judge": 0}

    async def _judge(*a, **k):
        called["judge"] += 1
        return {"conflicts": ""}

    plugin, logger = _make_plugin(
        consistency_check=True, host_personality="", judge=_judge
    )
    asyncio.run(plugin._check_anchor_consistency())
    assert called["judge"] == 0


def test_read_config_failure_degrades_silently():
    """读宿主配置失败 → 降级 debug，不抛（绝不影响加载）。"""
    plugin, logger = _make_plugin(consistency_check=True, raise_on_get=True)
    asyncio.run(plugin._check_anchor_consistency())  # 不得抛
    assert not logger.warnings


def test_judge_failure_degrades_silently():
    """LLM 比对失败 → 返回 None → 不 WARN 不 INFO。"""
    plugin, logger = _make_plugin(consistency_check=True, judge=None)
    # 未打桩 → 走真实 _judge_anchor_consistency；CreatorClient 在测试环境会失败
    asyncio.run(plugin._check_anchor_consistency())
    assert not logger.warnings


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
