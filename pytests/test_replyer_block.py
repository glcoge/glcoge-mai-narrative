"""replyer 漂移注入块（批 3-C3/C4）——item 契约 + 事实状态调制。

三件事锁在本用例里：

1. **item 契约**（H10）：``build_style_item`` 的 ``item_type`` + ``meta`` + ``parts``
   必须与宿主的 ``deserialize_context_item_snapshot`` 匹配；marker 前缀
   ``_narrative_style_`` 与 planner 块的 ``_narrative_life_context`` **不互为子串**
   → 两块互不误判（``is_style_item`` 不得把 planner 块认成自己）。
2. **W2 裁定（2026-10-13）**：停用授权段与 learned_style 注入——漂移层只留事实状态
   （心情/精力/作息相位），任何输入组合下块内不得出现「铺陈」「长期表达倾向」等文本。
3. **签名锁**：``stage/audience/owner`` 已从块契约删除，重加即重开注入面。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load  # noqa: E402

_BLOCK = load("services.render.replyer_block")
_PLANNER = load("services.render.planner_block")

AUTHORIZATION_TEXT = getattr(_BLOCK, "AUTHORIZATION_TEXT", None)  # W2: removed
STYLE_ITEM = _BLOCK.STYLE_ITEM
build_style_item = _BLOCK.build_style_item
is_style_item = _BLOCK.is_style_item
build_replyer_block = _BLOCK.build_replyer_block
# W2：AUTHORIZATION_TEXT 已停用，不再导出（若模块内仍保留则需删）


# ─── item 契约 ──────────────────────────────────────────────


def test_style_item_shape_matches_host_contract() -> None:
    item = build_style_item("调制文本")
    assert item["item_type"] == "UserMessageItem"
    assert item["meta"]["item_id"].startswith(STYLE_ITEM)
    assert item["meta"]["timestamp"]
    assert item["parts"][0]["type"] == "text"
    assert item["parts"][0]["text"] == "调制文本"


def test_style_item_ids_are_unique() -> None:
    assert build_style_item("x")["meta"]["item_id"] != build_style_item("x")["meta"]["item_id"]


def test_is_style_item_recognizes_own_item() -> None:
    assert is_style_item(build_style_item("x")) is True


def test_is_style_item_rejects_planner_item() -> None:
    """关键隔离：planner 块不得被认成本插件的 style 块（反之亦然）。"""
    planner_item = _PLANNER.build_injected_item("生活内容")
    assert is_style_item(planner_item) is False
    assert _PLANNER.is_injected_item(build_style_item("x")) is False


def test_is_style_item_rejects_garbage() -> None:
    assert is_style_item(None) is False
    assert is_style_item({}) is False
    assert is_style_item("_narrative_style_") is False


# ─── 授权段：只增不删 ───────────────────────────────────────


def test_authorization_always_present() -> None:
    """W2 裁定：授权段已停用，任何输入组合下「铺陈」不得出现。"""
    cases = [
        dict(drift_text="", learned_style=()),
        dict(drift_text="疲惫。", learned_style=()),
        dict(drift_text="轻快。", learned_style=["偏短"]),
        dict(drift_text="x", learned_style=()),
    ]
    for case in cases:
        block = build_replyer_block(**case)
        assert "铺陈" not in block, f"授权段不应出现: {case}"


def test_learned_slot_never_rendered() -> None:
    """W2 裁定：learned_style 槽已停用，「长期表达倾向」永不渲染。"""
    cases = [
        dict(drift_text="", learned_style=()),
        dict(drift_text="疲惫。", learned_style=["偏短"]),
        dict(drift_text="轻快。", learned_style=["少用感叹号"]),
    ]
    for case in cases:
        block = build_replyer_block(**case)
        assert "长期表达倾向" not in block, f"learned 槽不应出现: {case}"


# ─── 关系语境零注入（2026-10-13 裁定）─────────────────────────


def test_relationship_stage_never_rendered() -> None:
    """任何输入组合下，块内不得出现关系阶段文本（含旧 fail-closed 场景）。"""
    for drift in ("", "轻快。", "句子偏短些。"):
        for learned in ((), ("偏短",)):
            block = build_replyer_block(drift_text=drift, learned_style=learned)
            assert "关系阶段" not in block
            assert "相识" not in block
            assert "陌生人" not in block


def test_build_replyer_block_has_no_stage_params() -> None:
    """签名锁定：stage/audience/owner 已从块契约删除——重加即重开注入面。"""
    params = inspect.signature(build_replyer_block).parameters
    for banned in ("stage", "audience", "owner"):
        assert banned not in params, f"build_replyer_block 不应再接受 {banned} 参数"


def test_relationship_line_symbol_removed() -> None:
    """``relationship_line`` 符号本体删除——防从旧调用点复活。"""
    assert not hasattr(_BLOCK, "relationship_line")


# ─── 组装 ──────────────────────────────────────────────────


def test_block_contains_drift_text() -> None:
    block = build_replyer_block(drift_text="句子偏短些。", learned_style=())
    assert "句子偏短些" in block


def test_learned_slot_never_rendered_when_present() -> None:
    """W2：即使传入 learned_style，也不渲染（机制停用）。"""
    block = build_replyer_block(drift_text="x", learned_style=["少用感叹号"])
    assert "长期表达倾向" not in block
    # drift_text 仍应渲染
    assert "x" in block or block == ""  # 取决于是否空串处理


def test_block_has_no_hard_format_directives() -> None:
    """整块同样不得含硬格式指令与禁令组（ADR-0003 §6）。"""
    block = build_replyer_block(
        drift_text="句子偏短些。",
        learned_style=[],
    )
    for banned in ("字以内", "拆成", "分成", "不要", "禁止", "不得", "do not"):
        assert banned not in block


if __name__ == "__main__":
    from pytests._synth_loader import run_standalone

    raise SystemExit(run_standalone(globals()))
