"""replyer 漂移注入 hook —— 合同级 E2E（批 3-C3 / C7，E1 裁定）。

E1 硬条件：真 WS 灌帧 + 本地全栈**已移回「批收尾里程碑」**（ADR-0005 原计划），
本批闸门改用**合同级 E2E**——载荷字段与宿主 spec **逐字对齐**，直接调 handler，
断言 items / kwargs / 幂等。

载荷口径来源：``src/maisaka/chat_loop_service.py:353-436`` 的
``maisaka.replyer.before_model_request`` HookSpec（**15 个 required 字段**，
``default_timeout_ms=6000``、``allow_kwargs_mutation=True``）。

⚠️ 本文件不 import ``src.*``（插件纪律），宿主侧的 item 反序列化 / 校验规则
以 ``_assert_host_contract`` 内联复刻：
``deserialize_context_item_snapshot``（item_type + meta + parts）+
``validate_context_items(REQUEST_CONTEXT)``（item_id 唯一）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load, null_logger  # noqa: E402

load("services")
_PLUGIN = load("plugin")
MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin

#: 宿主 HookSpec 的 required 字段集（逐字抄自 chat_loop_service.py:419-435）
REQUIRED_FIELDS = (
    "items", "item_schema_version", "session_id", "request_type", "task_name",
    "requested_model_name", "selected_model_name", "selected_model_visual",
    "attempt", "retry_count", "max_retries", "reply_message_id", "reply_reason",
    "selected_expression_ids", "reply_tool_args",
)

CONTEXT_ITEM_SCHEMA_VERSION = 1
MODE_UID = "10001"
MODE_STREAM = f"stream-{MODE_UID}"

CONFIG_TEMPLATE = """\
[plugin]
enabled = true
[identity]
world = ""
values = []
world_rules = []
[learned]
style = []
"""


def _make_plugin(tmp_path: Path, *, narrative_enabled: bool = True, style: list | None = None):
    """构造绕过 __init__ 的插件实例；``_config_path`` 指向临时 config.toml。"""
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    plugin._plugin_config_instance = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=narrative_enabled,
            mode_user_ids=[MODE_UID],
            mode_stream_ids=[],
            timezone_offset_hours=8,
        ),
    )
    plugin._ctx = SimpleNamespace(logger=null_logger())
    # stream_id → uid（真实 StreamRegistry 的语义；H8：hook 传的 session_id 即 stream_id）
    plugin._streams = SimpleNamespace(
        uid_of=lambda sid: MODE_UID if sid == MODE_STREAM else ""
    )
    plugin._engine = SimpleNamespace(
        load_self_state=lambda: {
            "state": {
                "mood": {"label": "轻快", "energy": 0.8},
                "routine": {"phase": "上午", "sleep_state": "awake", "woken_count": 0},
            }
        },
        load_branch_state=lambda uid: {"relationship": {"stage": "相识"}},
    )
    plugin._store = SimpleNamespace()

    config_file = tmp_path / "config.toml"
    text = CONFIG_TEMPLATE
    if style is not None:
        text = text.replace("style = []", "style = [" + ", ".join(f'"{s}"' for s in style) + "]")
    config_file.write_text(text, encoding="utf-8")
    plugin._config_path = lambda: config_file  # 实例属性覆盖方法
    return plugin


def _payload(items: list, session_id: str = MODE_STREAM) -> dict:
    """按宿主 spec 构造真实载荷（15 个 required 字段一个不少）。"""
    return {
        "items": items,
        "item_schema_version": CONTEXT_ITEM_SCHEMA_VERSION,
        "session_id": session_id,
        "request_type": "reply",
        "task_name": "replyer",
        "requested_model_name": "",
        "selected_model_name": "demo-model",
        "selected_model_visual": False,
        "attempt": 1,
        "retry_count": 0,
        "max_retries": 2,
        "reply_message_id": "msg-1",
        "reply_reason": "用户提问",
        "selected_expression_ids": [],
        "reply_tool_args": {},
    }


def _assert_host_contract(item: dict) -> None:
    """内联复刻宿主反序列化/校验规则（deserialize_context_item_snapshot + validate）。"""
    assert isinstance(item, dict)
    assert isinstance(item.get("item_type"), str) and item["item_type"]
    meta = item.get("meta")
    assert isinstance(meta, dict)
    assert isinstance(meta.get("item_id"), str) and meta["item_id"]
    parts = item.get("parts")
    assert isinstance(parts, list) and parts
    for part in parts:
        assert isinstance(part, dict)
        assert part.get("type") == "text"
        assert isinstance(part.get("text"), str)


def _run(plugin, payload):
    import asyncio

    return asyncio.run(plugin.inject_drift_style(**payload))


# ─── 注入生效 ───────────────────────────────────────────────


def test_injects_style_item(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    payload = _payload([{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}])
    result = _run(plugin, payload)
    assert result["action"] == "continue"
    items = result["modified_kwargs"]["items"]
    assert len(items) == 2
    injected = items[-1]
    _assert_host_contract(injected)
    assert injected["meta"]["item_id"].startswith("_narrative_style_")
    text = injected["parts"][0]["text"]
    assert "舒展" in text  # 调制段（energy=0.8 → 轻快档）
    assert "关系阶段：相识" in text
    assert "铺陈" in text  # 授权段（C6 只增不删）


def test_kwargs_round_trip_no_loss(tmp_path: Path) -> None:
    """❗宿主 modified_kwargs 是整体替换非合并 → 15 个字段一个都不能少。"""
    plugin = _make_plugin(tmp_path)
    payload = _payload([{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}])
    original = {key: payload[key] for key in REQUIRED_FIELDS if key != "items"}
    result = _run(plugin, payload)
    returned = result["modified_kwargs"]
    for key in REQUIRED_FIELDS:
        assert key in returned, f"kwargs 丢失字段: {key}"
    for key, value in original.items():
        assert returned[key] == value, f"kwargs 字段被改写: {key}"


def test_item_ids_unique_across_calls(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    base = [{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}]
    first = _run(plugin, _payload(list(base)))["modified_kwargs"]["items"][-1]
    second = _run(plugin, _payload(list(base)))["modified_kwargs"]["items"][-1]
    assert first["meta"]["item_id"] != second["meta"]["item_id"]


def test_inject_counter_increments(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    base = [{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}]
    assert plugin._style_inject_count == 0
    _run(plugin, _payload(list(base)))
    _run(plugin, _payload(list(base)))
    assert plugin._style_inject_count == 2


# ─── 幂等 / 门禁 ────────────────────────────────────────────


def test_idempotent_when_style_item_present(tmp_path: Path) -> None:
    """同轮已有本块 → 不再追加（多 handler / 重入防护）。"""
    plugin = _make_plugin(tmp_path)
    items = [
        {"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []},
        {"item_type": "UserMessageItem", "meta": {"item_id": "_narrative_style_:x"}, "parts": []},
    ]
    result = _run(plugin, _payload(items))
    assert len(result["modified_kwargs"]["items"]) == 2


def test_gate_disabled_leaves_untouched(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, narrative_enabled=False)
    payload = _payload([{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}])
    result = _run(plugin, payload)
    assert len(result["modified_kwargs"]["items"]) == 1


def test_non_mode_session_leaves_untouched(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    payload = _payload(
        [{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}],
        session_id="stream-other",
    )
    result = _run(plugin, payload)
    assert len(result["modified_kwargs"]["items"]) == 1


def test_empty_items_leaves_untouched(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    result = _run(plugin, _payload([]))
    assert result["modified_kwargs"]["items"] == []


# ─── learned 槽 ─────────────────────────────────────────────


def test_learned_slot_absent_when_empty(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path)
    base = [{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}]
    text = _run(plugin, _payload(list(base)))["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    assert "长期表达倾向" not in text


def test_learned_slot_injected_when_present(tmp_path: Path) -> None:
    plugin = _make_plugin(tmp_path, style=["少用感叹号"])
    base = [{"item_type": "SystemMessageItem", "meta": {"item_id": "s1"}, "parts": []}]
    text = _run(plugin, _payload(list(base)))["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    assert "长期表达倾向" in text
    assert "少用感叹号" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
