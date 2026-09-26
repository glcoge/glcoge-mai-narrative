"""回放台：漂移注入块护栏用例（批 3-C6 / C7，ADR-0003 §6）。

## 为什么要这个用例

ADR-0003 §6 要求「铺陈 / 文学授权段**只增不删**」，且任何提示词精简 / 压缩改动都必须
过**注入前后对照样本**。HDSI 1.7B/C 的教训是：每次压缩都会**不可见地**砍掉文学授权，
正文随之干瘪——单元测试能守住函数实现，却守不住「未来有人为了省 token 顺手删掉它」。

故本用例把注入块当成**产物**来对照，而不是当成函数来调：

1. **授权段恒定**：穷举漂移层状态矩阵（4 心情 × 6 相位 × 3 睡眠态 = **72 组合**），
   断言授权段在**每一种**组合下都出现在注入块中；
2. **护栏不越界**：整块不含硬格式指令（拆气泡 / 字数 / 上限）与 Do-not 禁令组
   ——后者本身就是写作模板（HDSI「五步仪式」事故根因）；
3. **数据源健康**：归档读不出消息 / 会话即判失败（数据源问题，不是护栏问题）。

## 断言（空列表 = 通过）
"""

from __future__ import annotations

from typing import List, Tuple

from .. import loader
from ..runtime import load

#: 心情 × 精力（四档全覆盖）
_MOODS: Tuple[Tuple[str, float], ...] = (
    ("轻快", 0.9),
    ("平静", 0.5),
    ("低落", 0.3),
    ("疲惫", 0.1),
)

#: 作息相位（全量，含未定义值不该出现的边界）
_PHASES: Tuple[str, ...] = ("清晨", "上午", "午后", "下午", "晚间", "深夜")

#: 睡眠态：(sleep_state, woken_count)
_SLEEPS: Tuple[Tuple[str, int], ...] = (("awake", 0), ("awake", 2), ("asleep", 0))

_EXPECTED_COMBOS = len(_MOODS) * len(_PHASES) * len(_SLEEPS)

#: 硬格式指令（ADR-0003 §6 明令排除）
_BANNED_HARD: Tuple[str, ...] = ("字以内", "句以内", "拆成", "分成", "上限")
#: Do-not 禁令组（禁止性说明文本本身就是写作模板）
_BANNED_NEGATION: Tuple[str, ...] = ("不要", "禁止", "不得", "do not", "Do not")


def case_style_injection(messages_path: str) -> List[str]:
    """漂移注入块护栏：授权段恒定 + 无硬指令 / 禁令组。返回失败列表（空 = 通过）。"""
    failures: List[str] = []

    # 数据源健康（负控：空归档 / 读不出会话必须判失败）
    messages = loader.load_messages(messages_path)
    if not messages:
        return ["归档为空，无法验证（数据源问题，非护栏问题）"]
    if not loader.sessions(messages):
        return ["归档未解析出任何会话（数据源问题）"]

    drift = load("services.learning.drift_style")
    block_module = load("services.render.replyer_block")

    combos = 0
    for label, energy in _MOODS:
        for phase in _PHASES:
            for sleep_state, woken in _SLEEPS:
                combos += 1
                inner = {
                    "mood": {"label": label, "energy": energy},
                    "routine": {
                        "phase": phase,
                        "sleep_state": sleep_state,
                        "woken_count": woken,
                    },
                }
                tag = f"{label}/{phase}/{sleep_state}/woken={woken}"
                text = block_module.build_replyer_block(
                    drift_text=drift.describe_drift(inner),
                    stage="相识",
                    audience="u1",
                    owner="u1",
                    learned_style=(),
                )
                if not text.strip():
                    failures.append(f"注入块为空（{tag}）")
                    continue
                if block_module.AUTHORIZATION_TEXT not in text:
                    failures.append(f"授权段缺失（{tag}）")
                for banned in _BANNED_HARD + _BANNED_NEGATION:
                    if banned in text:
                        failures.append(f"注入块出现越界词「{banned}」（{tag}）")

    if combos != _EXPECTED_COMBOS:
        failures.append(f"组合矩阵覆盖不符：{combos} != {_EXPECTED_COMBOS}")
    return failures
