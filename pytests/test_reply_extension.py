"""REPLY_EXTENSION 回复扩展测试（v0.3.0 批 4 / R39 / 宿主 1.3.5 通道）。

合同以宿主源码为准（``src/plugin_runtime/host/reply_extensions.py``，该接口
有实现无文档）：
- prepare **只允许**返回 ``{"extra_prompt": str}``（:148 键集合校验）；
- 扩展异常 = 整次 reply 失败（:193-204）⇒ 插件 handler **全体自捕获**降级 `{}`；
- before_send 本批不使用（出站不动），返回 `{}` = 空操作（:203）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_reply_extension.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import _synth_loader
from pytests._synth_loader import load  # noqa: E402

_synth_loader.load("services")  # 先执行 services/__init__（plugin.py 依赖其再导出）
_PLUGIN = load("plugin")
_SCHEDULER = load("services.proactive.scheduler")

MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin
ProactiveScheduler = _SCHEDULER.ProactiveScheduler
with_reply_extension_hint = _SCHEDULER.with_reply_extension_hint

_NOW = datetime.datetime(2026, 10, 7, 20, 0, 0)
_FULL_NAME = "glcoge.mai-narrative.proactive_bysource"
_BY_SOURCE = "最近一段生活：煮了粥，有点烫。"


class _Logger:
    def __init__(self):
        self.errors: list = []

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        self.errors.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))


def _make_plugin(*, enabled=True, bysource=_BY_SOURCE, broken_scheduler=False):
    """回复扩展 handler 所需的最小插件（prepare/before_send 合同验证）。"""
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    logger = _Logger()
    plugin._plugin_config_instance = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        proactive=SimpleNamespace(
            enabled=True,
            reply_extension_enabled=enabled,
        ),
    )
    plugin._ctx = SimpleNamespace(logger=logger)
    if broken_scheduler:
        scheduler = SimpleNamespace(
            bysource_for_reply=lambda sid, now: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    else:
        scheduler = SimpleNamespace(
            bysource_for_reply=lambda sid, now: bysource if sid == "s1" else ""
        )
    plugin._proactive = scheduler
    plugin._local_now = lambda: _NOW
    plugin.reply_extension_full_name = _FULL_NAME
    return plugin


def _prepare(plugin, *, session_id="s1", phase="prepare"):
    return asyncio.run(
        plugin._reply_ext_proactive_bysource(
            phase=phase,
            session_id=session_id,
            reply_id="r1",
            call_id="c1",
            reply_message_id="m1",
            chat={},
            parameters={_FULL_NAME: {}},
            text="",
            messages=[],
        )
    )


# ===== 组件声明合同 =====


def test_component_declared_with_host_contract():
    """声明形状：name / chat_scope=private / parameters_schema = 空 object
    （模拟宿主 validate_parameter_schema 的接受形状）。"""
    handler = MaiNarrativePlugin._reply_ext_proactive_bysource
    info = getattr(handler, "__maibot_component_info__", None)
    assert info is not None, "组件信息未挂载（宿主注册不到）"
    assert info.name == "proactive_bysource"
    assert str(getattr(info, "chat_scope", "")).lower() == "private"
    schema = info.parameters_schema
    assert schema.get("type") == "object"
    assert schema.get("additionalProperties") is False
    assert schema.get("properties") == {}


# ===== prepare 合同与行为 =====


def test_prepare_returns_only_extra_prompt():
    """宿主 :148 逐字对齐：返回键集合 ⊆ {extra_prompt} 且值为字符串。"""
    plugin = _make_plugin()
    result = _prepare(plugin)
    assert set(result) - {"extra_prompt"} == set(), "prepare 只能返回 extra_prompt（宿主硬校验）"
    assert isinstance(result.get("extra_prompt", ""), str)


def test_prepare_carries_bysource_and_catch_instruction():
    """窗内有由头 → extra_prompt 含由头文本与承接指令（不复述/不解释是主动消息）。"""
    plugin = _make_plugin()
    prompt = _prepare(plugin)["extra_prompt"]
    assert _BY_SOURCE in prompt
    assert "不要逐字复述" in prompt
    assert "不要解释这是主动消息" in prompt


def test_prepare_empty_without_bysource():
    """窗内无由头 → {}（宿主空操作语义）。"""
    plugin = _make_plugin(bysource="")
    assert _prepare(plugin) == {}


def test_prepare_other_session_empty():
    """session 不匹配 → {}（别的会话的由头不外借）。"""
    plugin = _make_plugin()
    assert _prepare(plugin, session_id="s-other") == {}


def test_prepare_disabled_empty():
    """[proactive].reply_extension_enabled=false（默认）→ {}（零行为）。"""
    plugin = _make_plugin(enabled=False)
    assert _prepare(plugin) == {}


def test_prepare_scheduler_crash_degrades_to_empty():
    """🔴 扩展异常 = 整次 reply 失败（宿主语义）→ 任何内部异常全体自捕获降级 {}。"""
    plugin = _make_plugin(broken_scheduler=True)
    assert _prepare(plugin) == {}
    assert plugin._ctx.logger.errors, "降级必须记 ERROR（不静默）"


def test_before_send_returns_empty():
    """before_send 本批不使用：恒 {}（出站消息不动）。"""
    plugin = _make_plugin()
    assert _prepare(plugin, phase="before_send") == {}


def test_unknown_phase_returns_empty():
    """未知 phase → {}（宿主未来加 phase 时插件侧默认空操作）。"""
    plugin = _make_plugin()
    assert _prepare(plugin, phase="future_phase") == {}


# ===== 调度器：由头暂存查询（复用 _sent_records，不开新状态） =====


def _make_scheduler():
    sched = ProactiveScheduler.__new__(ProactiveScheduler)
    sched._plugin = SimpleNamespace(_local_now=lambda: _NOW)
    sched._task = None
    sched._running = False
    sched._next_fire = {}
    sched._sent_records = {}
    sched._pending_at = {}
    sched._engaged_windows = {}
    return sched


def test_bysource_for_reply_within_window():
    """record_sent 落下的由头，10 分钟窗内可查回。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "s1", _NOW, _BY_SOURCE)
    assert sched.bysource_for_reply("s1", _NOW + datetime.timedelta(minutes=2)) == _BY_SOURCE


def test_bysource_for_reply_latest_overwrites():
    """同流多次开口 → 取最新一条（覆盖语义）。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "s1", _NOW, "第一条由头")
    sched.record_sent("10001", "s1", _NOW + datetime.timedelta(minutes=1), "第二条由头")
    assert sched.bysource_for_reply("s1", _NOW + datetime.timedelta(minutes=2)) == "第二条由头"


def test_bysource_for_reply_beyond_window_empty():
    """超 10 分钟窗 → 空串（prepare 返回 {}，不陈旧提醒）。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "s1", _NOW, _BY_SOURCE)
    assert sched.bysource_for_reply("s1", _NOW + datetime.timedelta(minutes=11)) == ""


def test_bysource_for_reply_other_session_empty():
    """别的流的由头不外借。"""
    sched = _make_scheduler()
    sched.record_sent("10001", "s1", _NOW, _BY_SOURCE)
    assert sched.bysource_for_reply("s2", _NOW) == ""


# ===== reason 教学（模型如何选择扩展） =====


def test_reason_hint_when_enabled():
    """enabled → reason 追加 plugin_options 填写指引（含 full_name）。"""
    base = "由头文本\n（开口时必须调用 reply 工具并传入 msg_id…）"
    hinted = with_reply_extension_hint(base, _FULL_NAME, True)
    assert base in hinted
    assert f'plugin_options={{\"{_FULL_NAME}\": {{}}}}' in hinted


def test_reason_no_hint_when_disabled_or_unnamed():
    """disabled / full_name 空 → reason 原样（零行为）。"""
    base = "由头文本"
    assert with_reply_extension_hint(base, _FULL_NAME, False) == base
    assert with_reply_extension_hint(base, "", True) == base


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
