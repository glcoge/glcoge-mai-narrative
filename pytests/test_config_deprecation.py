"""弃用配置键告警测试（2026-09-13 体检 C6：creation_task → creation_model 迁移的防静默失效）。

背景：SDK 对未知配置键 extra="ignore" 静默丢弃——服务器 config.toml 若残留
``[llm].creation_task``，按模型名路由会静默失效（走默认模型）且无任何提示。
on_load 时经公开接口 get_plugin_config_data() 探测并 WARN 一次。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_config_deprecation.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_config_deprecation.py
"""

from __future__ import annotations

import logging as _stdlib_logging
import sys
from types import SimpleNamespace

import _synth_loader

_COMPONENT_INFO_ATTR = "__maibot_component_info__"

# 真正执行 services/__init__.py（plugin.py 依赖其再导出）
_synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin


class _ListHandler(_stdlib_logging.Handler):
    """捕获日志消息，供断言告警内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _make_plugin(*, raw_llm: dict) -> tuple:
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    plugin.get_plugin_config_data = lambda: {"llm": dict(raw_llm)}  # type: ignore[method-assign]
    logger = _stdlib_logging.Logger("deprecation-test", level=_stdlib_logging.DEBUG)
    handler = _ListHandler()
    logger.addHandler(handler)
    plugin._ctx = SimpleNamespace(logger=logger)
    return plugin, handler


def test_creation_task_presence_warns():
    """残留 [llm].creation_task → WARN 提示改用 creation_model。"""
    plugin, handler = _make_plugin(raw_llm={"creation_task": "learner"})

    plugin._warn_deprecated_config_keys()

    assert any("creation_task" in msg and "creation_model" in msg for msg in handler.messages), (
        f"应输出 creation_task 弃用告警（含迁移指引），实际日志: {handler.messages}"
    )


def test_creation_task_absent_no_warning():
    """已迁移（无旧键）→ 不告警。"""
    plugin, handler = _make_plugin(raw_llm={"creation_model": "my-model"})

    plugin._warn_deprecated_config_keys()

    assert not [m for m in handler.messages if "creation_task" in m]


def test_missing_llm_section_no_warning():
    """无 [llm] 段 / 非 dict → 安全跳过，不抛异常。"""
    plugin, handler = _make_plugin(raw_llm={})
    plugin.get_plugin_config_data = lambda: {}  # type: ignore[method-assign]

    plugin._warn_deprecated_config_keys()

    assert not [m for m in handler.messages if "creation_task" in m]


def test_component_info_unchanged():
    """哨兵：告警逻辑不得影响插件组件注册信息（防手滑改动 hook 声明）。"""
    assert hasattr(MaiNarrativePlugin.handle_post_send, _COMPONENT_INFO_ATTR)


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
