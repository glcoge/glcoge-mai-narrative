"""漂移层规则调制（批 3-C1 / ADR-0003 §1 §5 §6）——确定性映射表，零 LLM。

把**此刻状态**（精力 / 作息相位 / 睡眠态）翻成一句**倾向性软表述**，供 replyer 注入块使用。
对应 ADR-0002 三层表里的「漂移层 = 句长语气 / 话题偏好 / 作息 / 情绪精力」。

三条纪律（写死在代码里，不暴露成配置，防后续「精简」误删）：

1. **软表述，不是硬格式指令**。只说「句子偏短些、节奏更慢」这类倾向；
   绝不写「每句不超过 N 字」「拆成 N 条」「用 N 个气泡」——HDSI ChatRhythm 被整线废弃的
   正是**宿主检测器 + 硬格式指令**那一套，而句长/语气本身是漂移层的首要维度，不能跟着砍。
2. **禁 Do-not 禁令组**。禁止性说明文本本身就是写作模板（HDSI「五步仪式」事故根因）。
3. **作息相位只作映射输入，不输出时间名词**。相位决定节奏措辞，但不复述「现在是深夜」
   ——那条信息归 planner 块（E9 分工，防同一事实双重注入）。

措辞用**倾向**不用「上限」（HDSI「800 字上限被读成天花板、正文腰斩」事故）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

#: 输出长度上限（防注入膨胀；调制段是两三句短语，正常远低于此）
MODULATION_MAX_CHARS = 160

# 精力 → 句长/语气基调。倒序匹配（取最大的 threshold ≤ energy）——与引擎
# `mood_by_energy` 同款纪律，避免低能量误命中高能量档。        # OBSERVE(P14)
_MOOD_TONE: List[Tuple[float, str]] = [
    (0.72, "句子可以舒展些，愿意多写两笔，语气也轻快"),
    (0.40, "句子长短自然，语气平实"),
    (0.20, "句子偏短些，语气平缓，少些铺陈"),
    (0.00, "句子明显短些，气息收着，回应从简"),
]

# 心情标签 → 基调（仅在 energy 缺失/越界时兜底；正常走能量阈值）
_TONE_BY_LABEL: Dict[str, str] = {
    "轻快": _MOOD_TONE[0][1],
    "平静": _MOOD_TONE[1][1],
    "低落": _MOOD_TONE[2][1],
    "疲惫": _MOOD_TONE[3][1],
}
_TONE_DEFAULT = _MOOD_TONE[1][1]

# 作息相位 → 节奏倾向。**只输出调制效果，不复述时间**（见模块 docstring 纪律 3）
_PHASE_PACE: Dict[str, str] = {
    "清晨": "节奏放得更慢些",
    "上午": "节奏平稳",
    "午后": "节奏舒缓些",
    "下午": "节奏平稳",
    "晚间": "节奏舒缓些",
    "深夜": "节奏更慢",
}
_PHASE_PACE_FALLBACK = "节奏自然"

# 睡眠态修饰：睡着优先于「被吵醒计数」，两者不叠加
_ASLEEP_TONE = "睡得迷迷糊糊，断断续续，句子短"
_WOKEN_TONE = "刚被吵醒，还带着困意"


def _safe_int(value: Any) -> int:
    """把可能为 None / 脏字符串的计数值收敛成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tone_for_energy(energy: Any) -> Optional[str]:
    """按精力阈值取基调；energy 缺失或越界时返回 ``None``（交由标签兜底）。"""
    try:
        value = float(energy)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 1.0:
        return None
    for threshold, tone in _MOOD_TONE:
        if value >= threshold:
            return tone
    return _MOOD_TONE[-1][1]


def modulation_text(
    mood_label: str,
    energy: Any,
    phase: str,
    sleep_state: str = "awake",
    woken_count: Any = 0,
) -> str:
    """生成漂移层调制段（确定性，同输入必同输出）。

    Args:
        mood_label: 心情标签（``轻快/平静/低落/疲惫``），energy 异常时作兜底。
        energy: 精力值 0~1；缺失或越界时回落到 ``mood_label`` 基调。
        phase: 作息相位标签；未知值回落到中性节奏。
        sleep_state: ``asleep`` 时输出迷糊修饰。
        woken_count: 被吵醒次数，>0（且未睡着）时输出困意修饰。
    """
    tone = _tone_for_energy(energy)
    if tone is None:
        tone = _TONE_BY_LABEL.get(str(mood_label or ""), _TONE_DEFAULT)

    parts = [tone, _PHASE_PACE.get(str(phase or ""), _PHASE_PACE_FALLBACK)]
    if str(sleep_state or "") == "asleep":
        parts.append(_ASLEEP_TONE)
    elif _safe_int(woken_count) > 0:
        parts.append(_WOKEN_TONE)

    return ("；".join(parts) + "。")[:MODULATION_MAX_CHARS]


def describe_drift(inner_state: Mapping[str, Any]) -> str:
    """从 ``self_state["state"]`` 生成调制段（结构残缺也能产出合法文本）。

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
