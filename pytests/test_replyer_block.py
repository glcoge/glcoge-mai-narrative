"""replyer 漂移注入块（批 3-C3/C4/C6）——item 契约 + 授权段护栏 + 关系语境可见性。

三件事锁在本用例里：

1. **item 契约**（H10）：``build_style_item`` 的 ``item_type`` + ``meta`` + ``parts``
   必须与宿主的 ``deserialize_context_item_snapshot`` 匹配；marker 前缀
   ``_narrative_style_`` 与 planner 块的 ``_narrative_life_context`` **不互为子串**
   → 两块互不误判（``is_style_item`` 不得把 planner 块认成自己）。
2. **授权段只增不删**（C6 / ADR-0003 §6，HDSI 1.7B/C 教训）：任何输入组合下，
   ``AUTHORIZATION_TEXT`` 都必须出现在块里。
3. **关系语境可见性**（C4 / ADR-0004）：只对支线归属人本人可见（fail-closed）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load  # noqa: E402

_BLOCK = load("services.render.replyer_block")
_PLANNER = load("services.render.planner_block")

AUTHORIZATION_TEXT = _BLOCK.AUTHORIZATION_TEXT
STYLE_ITEM = _BLOCK.STYLE_ITEM
build_style_item = _BLOCK.build_style_item
is_style_item = _BLOCK.is_style_item
relationship_line = _BLOCK.relationship_line
build_replyer_block = _BLOCK.build_replyer_block


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
    """各种输入组合下授权段都在——这是 C6 的回归护栏。"""
    cases = [
        dict(drift_text="", stage="", audience="", owner="", learned_style=()),
        dict(drift_text="疲惫。", stage="陌生人", audience="u1", owner="u1", learned_style=()),
        dict(drift_text="轻快。", stage="老友", audience="u1", owner="u1", learned_style=["偏短"]),
        dict(drift_text="x", stage="相识", audience="g:123", owner="u1", learned_style=()),
    ]
    for case in cases:
        block = build_replyer_block(**case)
        assert AUTHORIZATION_TEXT in block, f"授权段缺失: {case}"


def test_authorization_text_is_narrative_only() -> None:
    """性质边界（用户纠偏）：只许叙事授权，不得越界人格约束。"""
    for banned in ("你是谁", "人格", "性格", "身份", "角色扮演", "不要", "禁止"):
        assert banned not in AUTHORIZATION_TEXT


# ─── 关系语境可见性 ─────────────────────────────────────────


def test_relationship_line_visible_to_owner() -> None:
    line = relationship_line("相识", audience="u1", owner="u1")
    assert "相识" in line


def test_relationship_line_hidden_from_others() -> None:
    """群聊/他人视角 → 不吐出关系（fail-closed）。"""
    assert relationship_line("相识", audience="g:999", owner="u1") == ""


def test_relationship_line_hidden_when_audience_empty() -> None:
    assert relationship_line("相识", audience="", owner="u1") == ""


def test_relationship_line_empty_stage_hidden() -> None:
    assert relationship_line("", audience="u1", owner="u1") == ""


# ─── 组装 ──────────────────────────────────────────────────


def test_block_contains_drift_text() -> None:
    block = build_replyer_block(
        drift_text="句子偏短些。", stage="", audience="", owner="", learned_style=()
    )
    assert "句子偏短些" in block


def test_learned_slot_absent_when_empty() -> None:
    """槽空 → 整段不渲染（不输出空标题）。"""
    block = build_replyer_block(
        drift_text="x", stage="", audience="", owner="", learned_style=()
    )
    assert "长期表达倾向" not in block


def test_learned_slot_rendered_when_present() -> None:
    block = build_replyer_block(
        drift_text="x", stage="", audience="", owner="", learned_style=["少用感叹号"]
    )
    assert "长期表达倾向" in block
    assert "少用感叹号" in block


def test_block_has_no_hard_format_directives() -> None:
    """整块同样不得含硬格式指令与禁令组（ADR-0003 §6）。"""
    block = build_replyer_block(
        drift_text="句子偏短些。", stage="老友", audience="u1", owner="u1",
        learned_style=["偏短"],
    )
    for banned in ("字以内", "拆成", "分成", "不要", "禁止", "不得", "do not"):
        assert banned not in block


if __name__ == "__main__":
    from pytests._synth_loader import run_standalone

    raise SystemExit(run_standalone(globals()))
