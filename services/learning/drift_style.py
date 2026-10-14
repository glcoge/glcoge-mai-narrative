"""漂移层状态事实段（批 3-C1 / ADR-0003 §1 §5 §6；2026-10-14 裁定改版）——确定性映射，零 LLM。

把**此刻状态**（心情 / 精力 / 睡眠态）翻成一句**纯事实表述**，供 replyer 注入块使用。

**2026-10-14 用户裁定：漂移层不注入风格类措辞**——原版的句长/语气/节奏「倾向性软表述」
（如「句子长短自然，语气平实；节奏平稳。」）整句移除，表达方式完全交给人设提示词；
漂移层只回答「此刻状态是什么」。作息相位仍归 planner 块（E9 分工不变，防同一事实双重注入）。

三条纪律（写死在代码里，不暴露成配置，防后续「精简」误删）：

1. **只输出事实，不输出表达指令**。不出现「句子 / 语气 / 节奏 / 铺陈」等风格词面，
   也不写「每句不超过 N 字」「拆成 N 条」这类硬格式指令（HDSI ChatRhythm 教训保留）。
2. **禁 Do-not 禁令组**。禁止性说明文本本身就是写作模板（HDSI「五步仪式」事故根因）。
3. **作息相位只作映射输入，不输出时间名词**。相位事实归 planner 块（E9 分工）。

历史注：原版输出「倾向性软表述」，W2（2026-10-13）删授权段与 learned_style；
本次裁定把调制措辞整体换成事实段。``modulation_text`` 函数名与签名保留以稳定调用方与测试
（``phase`` 参数接受但不再渲染）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, List, Optional

#: 输出长度上限（防注入膨胀；事实段是一句短语，正常远低于此）
MODULATION_MAX_CHARS = 160

# 睡眠态事实：睡着优先于「被吵醒计数」，两者不叠加
_ASLEEP_FACT = "睡梦中"
_WOKEN_FACT = "刚被吵醒"


def _safe_int(value: Any) -> int:
    """把可能为 None / 脏字符串的计数值收敛成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _valid_energy(energy: Any) -> Optional[float]:
    """校验精力值：可转 float 且在 [0,1] 内则返回，否则 ``None``（NaN 天然不入）。"""
    try:
        value = float(energy)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 1.0:
        return None
    return value


def modulation_text(
    mood_label: str,
    energy: Any,
    phase: str,
    sleep_state: str = "awake",
    woken_count: Any = 0,
) -> str:
    """生成漂移层事实段（确定性，同输入必同输出）。

    2026-10-14 裁定：只输出纯事实（心情 / 精力 / 睡眠态），不再输出句长语气 / 节奏等
    风格措辞；``phase`` 参数保留但**不再渲染**（相位/时间名词归 planner 块，E9）。
    无任何事实可输出时返回空串，调用方（``inject.py``）应跳过注入。

    Args:
        mood_label: 心情标签（``轻快/平静/低落/疲惫``）；空串则不输出心情事实。
        energy: 精力值 0~1；缺失/越界/NaN 则不输出精力事实。
        phase: 作息相位标签；**不渲染**（见模块 docstring 纪律 3）。
        sleep_state: ``asleep`` 时输出「睡梦中」事实。
        woken_count: 被吵醒次数，>0（且未睡着）时输出「刚被吵醒」事实。
    """
    parts: List[str] = []
    label = str(mood_label or "").strip()
    if label:
        parts.append(f"心情{label}")
    energy_value = _valid_energy(energy)
    if energy_value is not None:
        # 与 planner 块事实行同款渲染表达式（planner_block.build_context_block），两侧一致
        parts.append(f"精力 {energy_value * 10:.0f}/10")
    if str(sleep_state or "") == "asleep":
        parts.append(_ASLEEP_FACT)
    elif _safe_int(woken_count) > 0:
        parts.append(_WOKEN_FACT)

    if not parts:
        return ""
    return ("；".join(parts) + "。")[:MODULATION_MAX_CHARS]


def describe_drift(inner_state: Mapping[str, Any]) -> str:
    """从 ``self_state["state"]`` 生成事实段（结构残缺时产出空串，不崩）。

    Args:
        inner_state: 插件自我层的 ``state`` 内层（含 ``mood`` / ``routine`` 两段）。
    """
    mood = inner_state.get("mood") if isinstance(inner_state, Mapping) else None
    routine = inner_state.get("routine") if isinstance(inner_state, Mapping) else None
    mood = mood if isinstance(mood, Mapping) else {}
    routine = routine if isinstance(routine, Mapping) else {}
    return modulation_text(
        str(mood.get("label") or ""),
        mood.get("energy"),
        str(routine.get("phase") or ""),
        str(routine.get("sleep_state") or "awake"),
        routine.get("woken_count"),
    )
