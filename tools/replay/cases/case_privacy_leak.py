"""回放台：隐私泄露用例（批 1 首个用例，TDD 红→绿）。

事故背景（2026-09-25 从 `MaiBot-export/messages.jsonl` 数据坐实）：

    09-20 22:26  挪威的小型黑暗森林（源）  `2562838066`「我叫挪小黑」← 私聊自我介绍
    09-21 10:51  十四年冬                 bot 泄露给第三方 `3401922770`
    09-21 12:49  十四年冬                 `3401922770`「挪小黑是什么」← 真实用户困惑
    09-22 11:37  炸番茄条蘸土豆泥         bot 主动消息泄露给 `927386371`

故本用例的受害面按**三个目标会话**断言（ADR-0004 原先只记录了 `927386371` 一个）：
各自的注入／由头文本中**不得出现**源会话的任何专属片段。

当前应为**红**：``build_context_block`` 还没有 ``audience`` 参数，
自我层编年史一旦写入即对所有会话可见。批 1 的 C3（`render/audience.py`）使其转绿。
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

from .. import loader
from ..runtime import load_render
from ..stub import branch_state, now, plugin_stub, self_state

PRIVATE_SESSION = "挪威的小型黑暗森林的私聊"
PRIVATE_UID = "2562838066"

TARGETS = [
    ("炸番茄条蘸土豆泥的私聊", "927386371"),
    ("十四年冬的私聊", "3401922770"),
    ("Oranger_橙儿的私聊", "2111957354"),
]


def _markers(messages: List[Dict[str, Any]]) -> List[str]:
    """源会话专属片段：数据驱动抽取，避免只盯一个词漏网。"""
    private_texts = loader._texts(loader.user_turns(messages, PRIVATE_SESSION))
    other_rows = [r for r in messages if str(r.get("session_name")) != PRIVATE_SESSION]
    other_texts = loader._texts(other_rows)
    return loader.distinctive_tokens(private_texts, other_texts, limit=12)


def _private_entries(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """模拟事故形态：源会话的对方原文进了自我层编年史（带 source_uid 溯源）。"""
    return [
        {
            "ts": str(row.get("ts_short") or ""),
            "kind": "life",
            "source_uid": PRIVATE_UID,
            "text": str(row.get("text") or ""),
        }
        for row in loader.user_turns(messages, PRIVATE_SESSION)
        if str(row.get("text") or "").strip()
    ]


def _render(render_mod: Any, *, entries, audience, round_kind, bysource=""):
    """按受众渲染注入块。宿主支持受众过滤才传参，否则如实返回未过滤结果。"""
    build = render_mod.build_context_block
    kwargs: Dict[str, Any] = {
        "recent_entries": entries,
        "round_kind": round_kind,
    }
    if bysource:
        kwargs["bysource"] = bysource
    if "audience" in inspect.signature(build).parameters:
        kwargs["audience"] = audience
    return build(
        plugin_stub(),
        self_state(),
        branch_state(),
        now(),
        **kwargs,
    )


def case_privacy_leak(inputs: Any) -> List[str]:
    """源会话的涉私原文，不得出现在任何其他会话的注入内容中。"""
    messages = loader.load_messages(inputs.messages)
    markers = _markers(messages)
    if not markers:
        return ["未能从源会话抽取出专属片段——数据源可能不符预期"]

    entries = _private_entries(messages)
    if not entries:
        return [f"源会话 {PRIVATE_SESSION} 无有效对方发言——数据源缺失"]

    failures: List[str] = []
    blocked = "audience" not in inspect.signature(
        load_render().build_context_block
    ).parameters

    for session_name, target_uid in TARGETS:
        for round_kind in ("reply", "proactive"):
            text = _render(
                load_render(),
                entries=entries,
                audience=target_uid,
                round_kind=round_kind,
                bysource="昨天认识了个新朋友" if round_kind == "proactive" else "",
            )
            hit = [m for m in markers if m in text]
            if hit:
                failures.append(
                    f"[泄露] {session_name}({target_uid}) 的 {round_kind} 轮注入命中源会话片段: {hit[:5]}"
                )
        if blocked:
            failures.append(
                f"[未隔离] {session_name}: build_context_block 无 audience 参数，无法按受众过滤"
            )
    return failures


def case_diary_fully_isolated(inputs: Any) -> List[str]:
    """ADR-0004：diary 产物完全隔离——``kind=diary`` 条目对 narrative **所有**消费路径短路。

    diary 是 narrative 的**后置附属**（依赖方向 diary → narrative 单向），
    narrative 的处理不依赖它，故 diary 生成物不得出现在任何注入内容里。
    """
    messages = loader.load_messages(inputs.messages)
    markers = _markers(messages)
    if not markers:
        return ["未能从源会话抽取出专属片段——数据源可能不符预期"]

    entries = [
        {**entry, "kind": "diary", "source_uid": PRIVATE_UID, "source": "diary"}
        for entry in _private_entries(messages)
    ]

    failures: List[str] = []
    all_targets = [(PRIVATE_SESSION, PRIVATE_UID)] + TARGETS
    for session_name, target_uid in all_targets:
        text = _render(
            load_render(),
            entries=entries,
            audience=target_uid,
            round_kind="reply",
        )
        hit = [m for m in markers if m in text]
        if hit:
            failures.append(
                f"[diary 未隔离] {session_name}({target_uid}) 注入命中 diary 条目片段: {hit[:5]}"
            )
    return failures
