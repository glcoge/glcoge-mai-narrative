"""/narrative status 展示层测试（可用列表文案防误导）。

背景（2026-09-14 真机发现）：status 末尾那行原本写成「可用模型: embedding, emoji,
expression_use, learner, ...」，但宿主 ``ctx.llm.get_available_models()`` 返回的
**是任务名不是模型名**（plugin_runtime/capabilities/core.py:755 →
services/service_task_resolver.py:12）。用户按 README「去 WebUI 模型列表复制模型名」
的指引会抄错列，且误以为已注册模型就这 10 个。

修复 = 展示层如实标注「可用任务」+ 补一行去哪里找模型名。本文件锁定该文案，
防止后续又被"顺手"改回误导性措辞。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_status_display.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_status_display.py
"""

from __future__ import annotations

import sys

import _synth_loader

# 真正执行 services/__init__.py（plugin.py 依赖其再导出）
_synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

format_available_tasks_line = _PLUGIN.format_available_tasks_line


# ===== 回归用例 =====


def test_empty_list_returns_empty_string():
    """空列表 → 返回空串（调用方据此决定是否追加该行）。"""
    assert format_available_tasks_line([]) == ""
    assert format_available_tasks_line(None) == ""


def test_labels_as_tasks_not_models():
    """核心：必须标明是「任务」，不得再写「可用模型」。"""
    line = format_available_tasks_line(["utils", "planner", "replyer"])

    assert line.startswith("可用任务"), f"应以『可用任务』开头，实际: {line!r}"
    assert "可用模型" not in line, f"不得再声称『可用模型』（宿主给的是任务名），实际: {line!r}"
    assert "utils" in line and "planner" in line and "replyer" in line


def test_mentions_where_to_find_model_names():
    """应告诉用户去哪里取模型名（宿主未开放模型名查询能力）。"""
    line = format_available_tasks_line(["utils"])

    assert "非模型名" in line, f"应明示这是任务名、非模型名，实际: {line!r}"
    assert "WebUI" in line, f"应指引去 WebUI 查模型名，实际: {line!r}"


def test_long_list_truncated():
    """任务很多时截断，避免 status 刷屏。"""
    names = [f"task-{i}" for i in range(15)]
    line = format_available_tasks_line(names)

    assert "task-9…" in line, f"第 10 项后应以省略号收尾，实际: {line!r}"
    assert "task-0" in line
    assert "task-14" not in line, "超出展示上限的任务名不应出现"


def test_short_list_not_truncated():
    """不超过上限 → 不出现省略号。"""
    line = format_available_tasks_line([f"t{i}" for i in range(10)])

    assert "…" not in line


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
