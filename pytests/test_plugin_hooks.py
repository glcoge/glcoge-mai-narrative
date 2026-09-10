"""narrative 出站 hook 回归测试（hook 错挂修复 + rounds user_id + bot_msg_len 提取）。

背景（2026-09-08 排查定案）：出站采样挂在 ``send_service.before_send`` 上，但该
hook 的载荷**没有 stream_id**——rounds 配对与 ``_last_bot_sent`` 永远无法工作，
且 handler 疑似从未被派发（bot_msg_len 自上线起 0 条、user_initiated_freq 与
user_msg_len 完全 1:1）。修复 = 改挂 ``send_service.after_build_message``
（载荷含 stream_id，出站必经，派发点在外层 try/except 内不再静默）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_plugin_hooks.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_plugin_hooks.py
"""

from __future__ import annotations

import asyncio
import datetime
import importlib.util
import logging as _stdlib_logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SYNTH_PKG = "_narrative_hook_test_plugin"
_COMPONENT_INFO_ATTR = "__maibot_component_info__"


def _install_synth_package() -> None:
    if _SYNTH_PKG in sys.modules:
        return
    root = types.ModuleType(_SYNTH_PKG)
    root.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    sys.modules[_SYNTH_PKG] = root
    # 真正执行 services/__init__.py（plugin.py 依赖其再导出），子模块走标准导入机制
    _load("services", PLUGIN_ROOT / "services" / "__init__.py")


def _load(rel_name: str, file_path: Path):
    full_name = f"{_SYNTH_PKG}.{rel_name}"
    spec = importlib.util.spec_from_file_location(full_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {full_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_install_synth_package()
_PLUGIN = _load("plugin", PLUGIN_ROOT / "plugin.py")

MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin


class _FakeTelemetry:
    """记录 record() 调用的假 telemetry。"""

    def __init__(self) -> None:
        self.records: list = []

    def record(self, name: str, value: float = 1, user_id: str = "", scope: str = "") -> None:
        self.records.append(
            SimpleNamespace(name=name, value=value, user_id=user_id, scope=scope)
        )


def _make_plugin(*, narrative_enabled: bool = True) -> tuple:
    """构造绕过 __init__ 的插件实例 + 假 telemetry（纯 handler 单测所需最小依赖）。"""
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    # config 是 SDK 的只读 property（读 _plugin_config_instance），测试直接注入内层实例
    plugin._plugin_config_instance = SimpleNamespace(
        narrative=SimpleNamespace(
            enabled=narrative_enabled,
            timezone_offset_hours=8,
            mode_user_ids=["u1"],
            mode_stream_ids=[],
        ),
        plugin=SimpleNamespace(enabled=True),
        telemetry=SimpleNamespace(enabled=True),
    )
    telemetry = _FakeTelemetry()
    plugin._telemetry = telemetry
    # ctx 同为只读 property（读 self._ctx），直接注入内层
    plugin._ctx = SimpleNamespace(logger=_stdlib_logging.getLogger("narrative-hook-test"))
    plugin._stream_to_uid = {"s1": "u1"}
    plugin._last_bot_sent = {}
    plugin._pending_round = {}
    plugin._proactive = SimpleNamespace()  # handle_post_send 不使用
    return plugin, telemetry


def _payload(stream_id: str = "s1") -> dict:
    """构造 after_build_message 形态的载荷（message 为序列化 SessionMessage dict）。"""
    return {
        "stream_id": stream_id,
        "message": {
            "message_id": "m1",
            "message_info": {"user_info": {"user_id": "u1"}},
            "raw_message": [
                {"type": "text", "data": "对呀"},
            ],
        },
    }


# ===== 回归用例 =====


def test_outbound_hook_name_is_after_build_message():
    """出站采样必须挂在 after_build_message 上（before_send 载荷没有 stream_id）。"""
    info = getattr(MaiNarrativePlugin.handle_post_send, _COMPONENT_INFO_ATTR)
    assert info.hook == "send_service.after_build_message", (
        f"出站 hook 应挂 send_service.after_build_message，实际是 {info.hook}"
    )


def test_rounds_paired_with_user_id():
    """入站 30 分钟内的出站应配对 1 轮，且记录里带 user_id（按用户拆轮次的前提）。"""
    plugin, telemetry = _make_plugin()
    baseline = plugin._local_now()
    plugin._pending_round["s1"] = baseline - datetime.timedelta(minutes=1)

    asyncio.run(plugin.handle_post_send(**_payload()))

    rounds = [r for r in telemetry.records if r.scope == "rounds"]
    assert len(rounds) == 1, f"应配对 1 轮，实际 {len(rounds)}"
    assert rounds[0].value == 1
    assert rounds[0].user_id == "u1", f"rounds 应带 user_id=u1，实际 {rounds[0].user_id!r}"
    # 配对后待办应被消费
    assert "s1" not in plugin._pending_round
    # 出站时刻应更新 _last_bot_sent（指标 1 判定的依据）
    assert "s1" in plugin._last_bot_sent


def test_bot_msg_len_from_text_components():
    """bot_msg_len 应从 raw_message 的 text 组件提取（旧实现取不到会记出垃圾长度）。"""
    plugin, telemetry = _make_plugin()
    asyncio.run(plugin.handle_post_send(**_payload()))

    bot = [r for r in telemetry.records if r.scope == "bot_msg_len"]
    assert len(bot) == 1, f"应记录 1 条 bot_msg_len，实际 {len(bot)}"
    assert bot[0].value == 2.0, f"文本'对呀'长度应为 2，实际 {bot[0].value}"


def test_round_window_expiry():
    """入站超过 30 分钟窗口才出站 → 不配对（防超窗污染）。"""
    plugin, telemetry = _make_plugin()
    baseline = plugin._local_now()
    plugin._pending_round["s1"] = baseline - datetime.timedelta(minutes=40)

    asyncio.run(plugin.handle_post_send(**_payload()))

    assert not [r for r in telemetry.records if r.scope == "rounds"]
    assert "s1" not in plugin._pending_round  # 超窗的待办同样被消费丢弃


class _NoTouchEngine:
    """哨兵对象：narrative.enabled=false 时任何属性访问/调用都视为违规。"""

    def __getattr__(self, name: str):
        def _fail(*args, **kwargs):
            raise AssertionError(f"narrative.enabled=false 时不应调用 engine.{name}")

        return _fail


def test_injection_disabled_when_narrative_off():
    """A/B 对照关键用例：narrative.enabled=false 时注入必须停止。

    对照组窗口要求：剧本行为（注入/主动/创作/tick）全停，但入站/出站采样
    hook 不受影响（它们无本 gate）。mode 名单保留以维持采样。
    """
    plugin, _telemetry = _make_plugin(narrative_enabled=False)
    plugin._engine = _NoTouchEngine()
    plugin._store = _NoTouchEngine()

    items = [{"type": "text", "content": "用户消息占位"}]
    kwargs = {"session_id": "s1", "items": items}

    asyncio.run(plugin.inject_life_context(**kwargs))

    injected = [i for i in kwargs["items"] if _PLUGIN.is_injected_item(i)]
    assert not injected, "narrative.enabled=false 时不应注入剧本上下文"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    fns = [
        (name, obj)
        for name, obj in list(globals().items())
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
    sys.exit(0 if not failed else 1)
