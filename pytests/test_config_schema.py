"""插件配置在 WebUI 的渲染契约测试（2026-09-14 全量审计后的护栏）。

背景（真机 bug）：``[proactive].user_active_windows`` 是裸 ``dict``，WebUI 配置页
把它渲染成 ``[object Object]`` 的**单行输入框**，既看不懂也改不了。根因链路
（已逐行核实）：

- ``maibot_sdk/config.py:302 _build_field_schema`` → ``:515 _map_field_type``
  → ``:549 _default_ui_type``：裸 ``dict`` / ``dict[str, X]`` / ``Any`` 都落到
  ``default:`` 分支 → ``ui_type="text"``（或 "json"，但前端没有 json 分支）。
- 前端 ``dashboard/src/routes/plugin-config.tsx:178 FieldRenderer`` 只有
  **8 个分支**：``switch / number / slider / select / textarea / password /
  list / text(default)``。``text`` 用单行 ``<Input>`` 渲染，dict/JSON 直接变
  ``[object Object]``；多行字符串也会被挤成一行，保存即损坏配置。

本测试用 SDK 的**真实 schema 生成器**扫描全部字段，锁死三条不变量：

1. ``ui_type`` 必须落在前端支持的 8 个分支内；
2. 字段的 Python 类型不得映射为 ``object``（除非是 ``ui_type="list"`` 的对象数组，
   那种会被 ``ListFieldEditor`` 展开成带标签卡片行）；
3. 渲染成单行 ``text`` 的字段，其默认值**不得含换行**（否则保存即损坏）。

它是"别再犯同类错"的护栏：以后新增字段若又写成裸 ``dict``，这里会立刻红。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_config_schema.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_config_schema.py
"""

from __future__ import annotations

import sys

import _synth_loader

from maibot_sdk.config import generate_plugin_config_schema

_CONFIG = _synth_loader.load("config")

# 前端 FieldRenderer 支持的全部 ui_type（plugin-config.tsx:178，缺一即落 default 单行框）
_SUPPORTED_UI_TYPES = {
    "switch",
    "number",
    "slider",
    "select",
    "textarea",
    "password",
    "list",
    "text",
}


def _iter_fields(schema: dict):
    """展平 schema → [(section, field_name, field_schema), ...]。"""
    for section_name, section in schema.get("sections", {}).items():
        for field_name, field in (section.get("fields") or {}).items():
            yield section_name, field_name, field


def _all_fields():
    schema = generate_plugin_config_schema(_CONFIG.MaiNarrativePluginConfig)
    return list(_iter_fields(schema))


# ===== 不变量 1：ui_type 必须被前端支持 =====


def test_all_ui_types_are_supported_by_frontend():
    bad = [
        f"{sec}.{name}={field.get('ui_type')!r}"
        for sec, name, field in _all_fields()
        if field.get("ui_type") not in _SUPPORTED_UI_TYPES
    ]
    assert not bad, f"出现前端不支持的 ui_type（会退化成单行输入框）: {bad}"


# ===== 不变量 2：不得映射为 object（除非是对象数组 list） =====


def test_no_field_maps_to_object_type():
    """type="object" 且不是对象数组 → 前端渲染成 [object Object]，无法编辑。"""
    bad = [
        f"{sec}.{name} (ui_type={field.get('ui_type')!r}, item_type={field.get('item_type')!r})"
        for sec, name, field in _all_fields()
        if field.get("type") == "object" and field.get("ui_type") != "list"
    ]
    assert not bad, f"字段类型退化为 object（WebUI 显示 [object Object]）: {bad}"


def test_object_list_fields_carry_item_fields():
    """对象数组必须有 item_fields，否则 ListFieldEditor 无法展开成卡片行。"""
    bad = [
        f"{sec}.{name}"
        for sec, name, field in _all_fields()
        if field.get("ui_type") == "list"
        and field.get("item_type") == "object"
        and not field.get("item_fields")
    ]
    assert not bad, f"对象数组缺少 item_fields（前端无法渲染字段标签）: {bad}"


# ===== 不变量 3：单行框不得承载多行值 =====


def test_single_line_fields_have_no_newline_defaults():
    """ui_type=text 是单行 <Input>：默认值含换行会在保存时被挤掉。"""
    bad = []
    for sec, name, field in _all_fields():
        if field.get("ui_type") != "text":
            continue
        default = field.get("default")
        if isinstance(default, str) and "\n" in default:
            bad.append(f"{sec}.{name}")
    assert not bad, (
        f"以下字段用单行输入框但默认值含换行（保存即损坏），需加 x-widget=textarea: {bad}"
    )


# ===== 具体回归：按用户窗口规则必须渲染成对象数组卡片 =====


def test_user_window_rules_renders_as_object_list():
    """旧 user_active_windows（裸 dict）曾渲染成 [object Object]，现必须是对象数组。"""
    field = dict(
        (name, f) for _, name, f in _all_fields() if name == "user_window_rules"
    )["user_window_rules"]
    assert field["ui_type"] == "list"
    assert field["item_type"] == "object"
    assert set(field["item_fields"]) == {"user_id", "days", "start", "end"}


def test_user_window_rule_days_is_multi_select():
    """生效星期必须是多选下拉（1-7），而不是手填 JSON 数组。"""
    field = dict((name, f) for _, name, f in _all_fields() if name == "user_window_rules")[
        "user_window_rules"
    ]
    days = field["item_fields"]["days"]
    assert days["choices"] == ["1", "2", "3", "4", "5", "6", "7"]
    assert days.get("multiple") is True


def test_creator_api_key_is_masked():
    """直连 API Key 必须打码显示（x-widget=password；旧的 "password": True 是死元数据）。"""
    field = dict((name, f) for _, name, f in _all_fields() if name == "api_key")["api_key"]
    assert field["ui_type"] == "password"


def test_share_urge_fields_defaults_and_types():
    """v0.1.8 share_urge 六参数：存在、默认值正确、全部是 switch/number 可渲染类型。

    urge 参数直接决定主动开口频率（A/B 可调纪律），默认值被误改会让
    部署前后行为漂移无据可查，这里锁死出厂值。
    """
    fields = dict((name, f) for _, name, f in _all_fields())
    expected = {
        "urge_enabled": (True, "switch"),
        "urge_base": (0.7, "number"),
        "urge_gain": (0.1, "number"),
        "urge_decay": (0.25, "number"),
        "urge_regain": (0.15, "number"),
        "urge_branch_floor": (0.4, "number"),
    }
    missing = [name for name in expected if name not in fields]
    assert not missing, f"share_urge 字段缺失（可能被误删）: {missing}"
    for name, (default, ui_type) in expected.items():
        field = fields[name]
        assert field.get("default") == default, f"{name} 默认值漂移: {field.get('default')!r}"
        assert field.get("ui_type") == ui_type, f"{name} ui_type 异常: {field.get('ui_type')!r}"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
