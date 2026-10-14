"""漂移层状态事实段（2026-10-14 裁定改版）——确定性映射，零 LLM。

事实段把**此刻状态**（心情 / 精力 / 睡眠态）翻成一句**纯事实表述**，经 replyer 通道注入。

三条硬纪律锁在本用例里：

| 纪律 | 依据 | 断言 |
|---|---|---|
| **只输出事实，不输出表达指令**（禁风格词面） | 2026-10-14 用户裁定（W2「只留事实状态」意图补完）——原版句长/语气/节奏软表述整句移除 | 不含「句子/语气/节奏/铺陈」 |
| **禁 Do-not 禁令组** | ADR-0003 §6（HDSI「五步仪式」事故根因，保留） | 不含「不要/禁止/不得/do not」 |
| 作息相位只作**映射输入**，不输出、不渲染 | E9 分工（防与 planner 块重复，保留） | 不含「深夜/清晨/上午/午后/下午/晚间」，且相位不影响输出 |

另锁：事实渲染（心情标签/精力 N/10/睡眠态）、无事实返回空串、未知输入不崩、输出确定性、长度上限。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load  # noqa: E402

_DRIFT = load("services.learning.drift_style")

modulation_text = _DRIFT.modulation_text
describe_drift = _DRIFT.describe_drift
MODULATION_MAX_CHARS = _DRIFT.MODULATION_MAX_CHARS

#: 风格词面——2026-10-14 裁定：漂移层不得输出任何表达指令（回归锁）
_FORBIDDEN_STYLE_WORDS = ("句子", "语气", "节奏", "铺陈", "句长")
#: 硬格式指令关键词——出现即视为越界（ADR-0003 §6，保留）
_FORBIDDEN_HARD = ("条", "字数", "上限", "字以内", "句以内", "拆成", "分成", "模板")
#: 禁令组关键词——禁止性说明文本本身就是写作模板
_FORBIDDEN_NEGATION = ("不要", "禁止", "不得", "不能", "别", "do not", "Do not")
#: 作息时间名词——只作映射输入，不得出现在输出里（E9，保留）
_FORBIDDEN_TIME_WORDS = ("深夜", "清晨", "上午", "午后", "下午", "晚间")


def _assert_clean(text: str) -> None:
    for word in _FORBIDDEN_STYLE_WORDS:
        assert word not in text, f"事实段出现风格词面「{word}」: {text}"
    for word in _FORBIDDEN_HARD:
        assert word not in text, f"事实段出现硬格式指令「{word}」: {text}"
    for word in _FORBIDDEN_NEGATION:
        assert word not in text, f"事实段出现禁令组「{word}」: {text}"
    for word in _FORBIDDEN_TIME_WORDS:
        assert word not in text, f"事实段出现作息时间名词「{word}」: {text}"


# ─── 事实渲染：心情 + 精力 ──────────────────────────────────


def test_high_energy_renders_facts() -> None:
    text = modulation_text("轻快", 0.9, "上午")
    assert "心情轻快" in text
    assert "精力" in text
    _assert_clean(text)


def test_mid_energy_renders_facts() -> None:
    text = modulation_text("平静", 0.5, "下午")
    assert "心情平静" in text
    assert "精力 5/10" in text
    _assert_clean(text)


def test_low_energy_renders_facts() -> None:
    text = modulation_text("低落", 0.3, "下午")
    assert "心情低落" in text
    assert "精力 3/10" in text
    _assert_clean(text)


def test_very_low_energy_renders_facts() -> None:
    text = modulation_text("疲惫", 0.1, "深夜")
    assert "心情疲惫" in text
    assert "精力 1/10" in text
    _assert_clean(text)


def test_boundary_values_do_not_crash() -> None:
    for energy in (0.0, 1.0, 0.72, 0.40, 0.20):
        text = modulation_text("平静", energy, "上午")
        assert text
        assert "精力" in text
        _assert_clean(text)


# ─── 作息相位：不渲染（E9，相位事实归 planner） ───────────────


def test_phase_not_rendered() -> None:
    """相位不再影响输出（2026-10-14 裁定：原「相位→节奏措辞」映射整表移除）。"""
    day = modulation_text("平静", 0.5, "上午")
    night = modulation_text("平静", 0.5, "深夜")
    assert day == night, "相位不得影响事实段输出"
    _assert_clean(day)
    _assert_clean(night)


def test_unknown_phase_not_rendered() -> None:
    text = modulation_text("平静", 0.5, "子夜未定义")
    assert text == modulation_text("平静", 0.5, "上午")
    _assert_clean(text)


def test_missing_phase_not_rendered() -> None:
    text = modulation_text("平静", 0.5, "")
    assert text == modulation_text("平静", 0.5, "上午")
    _assert_clean(text)


# ─── 睡眠态 / 被吵醒（事实） ────────────────────────────────


def test_asleep_state_renders_fact() -> None:
    text = modulation_text("疲惫", 0.1, "深夜", sleep_state="asleep")
    assert "睡梦中" in text
    _assert_clean(text)


def test_woken_state_renders_fact() -> None:
    text = modulation_text("疲惫", 0.1, "深夜", sleep_state="awake", woken_count=1)
    assert "刚被吵醒" in text
    _assert_clean(text)


def test_asleep_takes_precedence_over_woken() -> None:
    """睡着优先于「被吵醒计数」，两者不重复叠加。"""
    text = modulation_text("疲惫", 0.1, "深夜", sleep_state="asleep", woken_count=2)
    assert "睡梦中" in text
    assert "刚被吵醒" not in text


# ─── 容错 / 确定性 / 长度 / 空串契约 ─────────────────────────


def test_nan_energy_omits_energy_fact() -> None:
    text = modulation_text("疲惫", float("nan"), "深夜")
    assert "心情疲惫" in text
    assert "精力" not in text
    _assert_clean(text)


def test_out_of_range_energy_omits_energy_fact() -> None:
    text = modulation_text("轻快", 9.9, "上午")
    assert "心情轻快" in text
    assert "精力" not in text
    _assert_clean(text)


def test_empty_state_returns_empty_string() -> None:
    """无任何事实（无标签+无效精力+无睡眠修饰）→ 空串，调用方应跳过注入。"""
    assert modulation_text("", None, "") == ""
    assert modulation_text("", float("nan"), "", sleep_state="awake", woken_count=0) == ""
    assert describe_drift({}) == ""


def test_label_only_renders_mood_fact() -> None:
    """精力缺失时只渲染心情事实。"""
    text = modulation_text("平静", None, "上午")
    assert text == "心情平静。"
    _assert_clean(text)


def test_deterministic() -> None:
    first = modulation_text("平静", 0.5, "晚间", sleep_state="awake", woken_count=0)
    second = modulation_text("平静", 0.5, "晚间", sleep_state="awake", woken_count=0)
    assert first == second


def test_length_within_cap() -> None:
    text = modulation_text("疲惫", 0.0, "深夜", sleep_state="asleep")
    assert len(text) <= MODULATION_MAX_CHARS


# ─── describe_drift：从 self_state['state'] 取字段 ────────────


def test_describe_drift_reads_nested_state() -> None:
    inner = {
        "mood": {"label": "轻快", "energy": 0.8},
        "routine": {"phase": "上午", "sleep_state": "awake", "woken_count": 0},
    }
    text = describe_drift(inner)
    assert "心情轻快" in text
    assert "精力 8/10" in text
    _assert_clean(text)


def test_describe_drift_missing_sections_return_empty() -> None:
    """state 结构残缺 → 返回空串（不崩、不产词），由 inject 层跳过注入。"""
    assert describe_drift({}) == ""


if __name__ == "__main__":
    from pytests._synth_loader import run_standalone

    raise SystemExit(run_standalone(globals()))
