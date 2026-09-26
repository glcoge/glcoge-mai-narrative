"""漂移层规则调制（批 3-C1 / ADR-0003 §1 §5 §6）——确定性映射，零 LLM。

调制段把**此刻状态**（精力 / 作息相位 / 睡眠态）翻成一句**倾向性软表述**，
经 replyer 通道注入。

三条硬纪律锁在本用例里：

| 纪律 | 依据 | 断言 |
|---|---|---|
| 句长/语气用**软表述**（区间、节奏感），不是硬格式指令 | ADR-0003 §6；E6 纠偏——ChatRhythm 废弃的是宿主检测器+硬格式指令，**不是软措辞** | 不含「条/字数/上限/字以内」等 |
| **禁 Do-not 禁令组** | ADR-0003 §6（HDSI「五步仪式」事故根因） | 不含「不要/禁止/不得/do not」 |
| 作息相位只作**映射输入**，不输出时间名词 | E9 分工（防与 planner 块重复） | 不含「深夜/清晨/上午/午后/下午/晚间」 |

另锁：四档能量基调、睡眠/被吵醒修饰、未知输入不崩、输出确定性、长度上限。
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

#: 硬格式指令 / 禁令组关键词——出现即视为越界（ADR-0003 §6）
_FORBIDDEN_HARD = ("条", "字数", "上限", "字以内", "句以内", "拆成", "分成", "模板")
#: 禁令组关键词——禁止性说明文本本身就是写作模板
_FORBIDDEN_NEGATION = ("不要", "禁止", "不得", "不能", "别", "do not", "Do not")
#: 作息时间名词——只作映射输入，不得出现在输出里（E9）
_FORBIDDEN_TIME_WORDS = ("深夜", "清晨", "上午", "午后", "下午", "晚间")


def _assert_clean(text: str) -> None:
    for word in _FORBIDDEN_HARD:
        assert word not in text, f"调制段出现硬格式指令「{word}」: {text}"
    for word in _FORBIDDEN_NEGATION:
        assert word not in text, f"调制段出现禁令组「{word}」: {text}"
    for word in _FORBIDDEN_TIME_WORDS:
        assert word not in text, f"调制段出现作息时间名词「{word}」: {text}"


# ─── 四档能量基调 ────────────────────────────────────────────


def test_high_energy_expands() -> None:
    text = modulation_text("轻快", 0.9, "上午")
    assert "舒展" in text
    _assert_clean(text)


def test_mid_energy_neutral() -> None:
    text = modulation_text("平静", 0.5, "下午")
    assert "自然" in text
    _assert_clean(text)


def test_low_energy_shortens() -> None:
    text = modulation_text("低落", 0.3, "下午")
    assert "偏短" in text
    _assert_clean(text)


def test_very_low_energy_shortens_more() -> None:
    text = modulation_text("疲惫", 0.1, "深夜")
    assert "明显短" in text
    _assert_clean(text)


def test_boundary_values_do_not_crash() -> None:
    for energy in (0.0, 1.0, 0.72, 0.40, 0.20):
        text = modulation_text("平静", energy, "上午")
        assert text
        _assert_clean(text)


# ─── 作息相位 → 节奏（只作映射输入） ─────────────────────────


def test_phase_changes_pace() -> None:
    day = modulation_text("平静", 0.5, "上午")
    night = modulation_text("平静", 0.5, "深夜")
    assert day != night, "不同相位应产出不同节奏措辞"
    _assert_clean(day)
    _assert_clean(night)


def test_unknown_phase_falls_back() -> None:
    text = modulation_text("平静", 0.5, "子夜未定义")
    assert text
    _assert_clean(text)


def test_missing_phase_falls_back() -> None:
    text = modulation_text("平静", 0.5, "")
    assert text
    _assert_clean(text)


# ─── 睡眠态 / 被吵醒 ────────────────────────────────────────


def test_asleep_state_marks_drowsy() -> None:
    text = modulation_text("疲惫", 0.1, "深夜", sleep_state="asleep")
    assert "迷糊" in text
    _assert_clean(text)


def test_woken_state_marks_sleepiness() -> None:
    text = modulation_text("疲惫", 0.1, "深夜", sleep_state="awake", woken_count=1)
    assert "困意" in text
    _assert_clean(text)


def test_asleep_takes_precedence_over_woken() -> None:
    """睡着优先于「被吵醒计数」，两者不重复叠加。"""
    text = modulation_text("疲惫", 0.1, "深夜", sleep_state="asleep", woken_count=2)
    assert "迷糊" in text
    assert "困意" not in text


# ─── 容错 / 确定性 / 长度 ───────────────────────────────────


def test_nan_energy_uses_label_fallback() -> None:
    text = modulation_text("疲惫", float("nan"), "深夜")
    assert text
    _assert_clean(text)


def test_out_of_range_energy_uses_label_fallback() -> None:
    text = modulation_text("轻快", 9.9, "上午")
    assert text
    assert "舒展" in text, "越界 energy 应回落到 label 基调"


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
    assert "舒展" in text
    _assert_clean(text)


def test_describe_drift_tolerates_missing_sections() -> None:
    """state 结构残缺 → 仍产出合法文本，不崩。"""
    text = describe_drift({})
    assert text
    _assert_clean(text)


if __name__ == "__main__":
    from pytests._synth_loader import run_standalone

    raise SystemExit(run_standalone(globals()))
