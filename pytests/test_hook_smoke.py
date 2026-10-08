"""Hook 转发壳冒烟（深化 C1 / Q8a）：注册存在 + **kwargs 整体透传契约 + AST 守卫。

深化 C1 把 5+1 个 hook/扩展的实现体下沉 services/{inbound,outbound,inject}.py，
plugin 侧只剩装饰器声明 + 一行转发壳。壳的两条铁律由本文件机器判定：

- 🔴 **``**kwargs`` 整体透传**：转发层逐键拆参的话，漏键的症状是「注入段/
  落痕静默消失」这类不报错失效——冒烟用 spy 断言**键集原样抵达 sink**；
- 🔴 **转发层不新增 try/except**：异常吞噬面保持现状（hook 的
  ``ErrorPolicy.SKIP`` 由装饰器声明承担，实现体内既有 try/except 随迁）——
  AST 断言壳体内零 ``Try`` 节点、形参只有 ``self`` 与一个 ``**`` 打包。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_hook_smoke.py -q
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

import _synth_loader

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# 先执行 services 包 __init__（空壳会顶掉真包——见 _synth_loader 注释），再载 plugin
_synth_loader.load("services")
_PLUGIN_MOD = _synth_loader.load("plugin")
MaiNarrativePlugin = _PLUGIN_MOD.MaiNarrativePlugin

_COMPONENT_INFO_ATTR = "__maibot_component_info__"

# (plugin 方法名, sink 所在模块属性名, sink 函数名, 透传形参名)
SHIMS = [
    ("handle_inbound_message", "inbound", "handle_inbound_message", "kwargs"),
    ("handle_post_send", "outbound", "handle_post_send", "kwargs"),
    ("inject_life_context", "inject", "inject_life_context", "kwargs"),
    ("inject_drift_style", "inject", "inject_drift_style", "kwargs"),
    ("block_expression_select", "inject", "block_expression_select", "kwargs"),
    ("block_expression_upsert", "inject", "block_expression_upsert", "kwargs"),
    ("_reply_ext_proactive_bysource", "inject", "reply_ext_proactive_bysource", "payload"),
]


def _plugin():
    """``__new__`` 裸实例：壳只透传，spy 替换 sink 后不触任何插件状态。"""
    return MaiNarrativePlugin.__new__(MaiNarrativePlugin)


# ─── 每装饰器 1 冒烟：注册存在 + 透传契约 ───────────────────────


@pytest.mark.parametrize(
    "method_name,mod_attr,sink_name,param_name",
    [(m, s, f, p) for m, s, f, p in SHIMS],
    ids=[m for m, _, _, _ in SHIMS],
)
def test_shim_registers_and_passes_whole_kwargs(monkeypatch, method_name, mod_attr, sink_name, param_name):
    """装饰器 info 在位 + 调用键集原样抵达 sink + 返回值原样带回。"""
    method = getattr(MaiNarrativePlugin, method_name)
    assert hasattr(method, _COMPONENT_INFO_ATTR), f"{method_name} 缺组件注册信息"

    captured = {}
    sentinel = {"action": "continue", "modified_kwargs": {"__spy__": True}}

    async def _spy(plugin, **kw):
        captured["plugin"] = plugin
        captured["kwargs"] = kw
        return sentinel

    monkeypatch.setattr(getattr(_PLUGIN_MOD, mod_attr), sink_name, _spy)

    payload = {"message": {"x": 1}, "stream_id": "s1", "session_id": "s1"}
    result = asyncio.run(getattr(_plugin(), method_name)(**payload))

    assert captured["kwargs"] == payload, "键集必须原样抵达 sink（🔴 不拆键）"
    assert captured["plugin"] is not None
    assert result is sentinel, "返回值必须原样带回"


def test_observe_group_shim_passes_positional(monkeypatch):
    """``_observe_group`` 同步壳：两参原样抵达 sink。"""
    captured = {}

    def _spy(plugin, message, stream_id):
        captured["args"] = (plugin, message, stream_id)
        return None

    monkeypatch.setattr(getattr(_PLUGIN_MOD, "inbound"), "observe_group", _spy)
    plugin = _plugin()
    message = {"group_info": {}}
    assert plugin._observe_group(message, "s1") is None

    assert captured["args"][1] is message and captured["args"][2] == "s1"


# ─── AST 守卫：壳体零 try/except、形参只允许 self + 单一 ** 打包 ──


def test_forwarding_shims_structurally_thin():
    """壳体结构守卫（机器判定两条铁律）：

    1. 壳函数体内**零 ``Try`` 节点**（异常吞噬面不因下沉扩大）；
    2. 形参只有 ``self`` 与一个 ``**`` 打包（结构性排除逐键拆参）；
    3. 函数体只有一条 return 转发（防壳内夹带逻辑）。
    """
    tree = ast.parse((PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8"))
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for method_name, _, sink_name, param_name in SHIMS:
        node = methods.get(method_name)
        assert node is not None, f"未找到壳方法 {method_name}"
        # 只走函数体（ast.walk(FunctionDef) 会把装饰器调用也算进来）
        body_nodes = [item for stmt in node.body for item in ast.walk(stmt)]
        for item in body_nodes:
            assert not isinstance(item, ast.Try), f"{method_name} 壳体内出现 try/except"
        positional = [a for a in node.args.args if a.arg != "self"]
        assert positional == [], f"{method_name} 壳不得声明逐键形参"
        packed = node.args.kwarg
        assert packed is not None and packed.arg == param_name, (
            f"{method_name} 壳必须只带 **{param_name} 整体透传"
        )
        calls = [n for n in body_nodes if isinstance(n, ast.Call)]
        assert len(calls) == 1, f"{method_name} 壳体只允许一条转发调用"
        starred = [
            kw for kw in calls[0].keywords if kw.arg is None
        ]
        assert len(starred) == 1 and starred[0].value.id == param_name, (
            f"{method_name} 转发必须 **{param_name} 整体透传"
        )

