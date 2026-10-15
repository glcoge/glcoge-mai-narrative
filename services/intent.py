"""意图账本（P2 / R45 / ADR-0006，2026-10-15 意图治理批）。

把「她对人的未来动作/挂念」从生活片段散文里拿出来，记成结构化账目
（kind=commitment 事件实体——批 0 预留的第三型在此激活），到期先带对话
事实重判再决定动不动。背景是 2026-10-14 催饭复读案：创作层素材把「要不要
催」预支成已定行动（「一会儿还是得发消息催催那家伙」），注入 planner 后被
照办——P1（COMM_FACT_RULE 收紧）堵了散文侧，本模块给挂念一个结构化去处。

# OBSERVE(R45)：意图账本单一实现（登记表 R45 行）——意图行解析、due 分档、
# 账本存取、到期重判裁决都只在本模块；消费端（scheduler._fire）与登记端
# （creation/life.py）只做调用。

四条设计红线（ADR-0006）：
1. 意图行只是创作层的登记通道，**不进片段正文、不进编年史**；
2. commitment **永不进 planner/replyer 注入块**——唯一消费口 = 主动轮由头，
   且由头形态是「一直惦记着X」的未定挂念（entry.text），不是「去催X」的
   指令（要不要行动、何时行动是 planner 在对话轮的裁决，P1 纪律在账本侧的延伸）；
3. 到期 ≠ 执行：重判先于一切动作；检索基础设施失败 = 推迟不裁（宁晚勿盲催）；
4. 账目落账三态（resolved / expired 都改写 status 并留指标，作废另留编年史痕），
   不静默丢弃。

存储：``focus.commitments``（state 内独立列表）——**不能进** pending_events
（那是 LRU 池，fragment_pending_max=12，隔夜即被新片段挤掉；承诺会活过隔夜）。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

from .creation.event_entity import KIND_COMMITMENT, make_commitment_event

__all__ = [
    "INTENT_LINE_MARK",
    "parse_intent_line",
    "resolve_due",
    "register_commitment",
    "pending_commitments",
    "commitment_status",
]


#: 意图行标记（正文后的账本登记行前缀；与 prompt 指令、测试三处同源）
INTENT_LINE_MARK = "[意图]"

#: 意图行「什么时候」字段的合法档位（LLM 不估具体时刻，只选档）
_WHEN_CHOICES = ("今晚", "明天", "这两天")


def parse_intent_line(text: str) -> Tuple[str, Optional[Dict[str, str]]]:
    """从生成文本里摘出意图行。返回 ``(剥离后的正文, 意图 dict 或 None)``。

    意图行格式（行首，允许缩进）：``[意图] 谁|惦记的具体的事|今晚/明天/这两天``。
    - 无意图行 → 原文原样返回；
    - 有行但字段不全/档位非法 → 行仍剥离（元数据绝不漏进正文），登记放弃；
    - 多行意图行只取**最后一行**（登记端每段至多一条，模型写多行以最后为准）。
    """
    body_lines: List[str] = []
    intent_part: Optional[str] = None
    for line in str(text or "").splitlines():
        if line.strip().startswith(INTENT_LINE_MARK):
            intent_part = line.strip()[len(INTENT_LINE_MARK):].strip()
            continue
        body_lines.append(line)
    body = "\n".join(body_lines)
    if intent_part is None:
        return str(text or ""), None

    parts = [p.strip() for p in intent_part.split("|")]
    if len(parts) != 3 or not all(parts):
        return body, None
    who, what, when = parts
    if not any(when == choice or when.startswith(choice) for choice in _WHEN_CHOICES):
        return body, None
    return body, {"who": who, "what": what, "when": when}


def resolve_due(when: str, now: datetime.datetime, sleep_time: str) -> str:
    """把档位词映射为 due ISO 串（秒级）。分档封顶裁定：LLM 不估具体时刻。

    - 今晚 → 当日睡前 1 小时（sleep_time 解析失败 → now+3h；已过 → now+1h）；
    - 明天 → 次日 14:00；
    - 这两天 → now+48h。
    """
    from .state.engine import parse_clock

    word = str(when or "").strip()
    if word.startswith("今晚"):
        base = parse_clock(sleep_time)
        if base is None:
            due = now + datetime.timedelta(hours=3)
        else:
            due = datetime.datetime.combine(now.date(), base) - datetime.timedelta(hours=1)
        if due <= now:
            due = now + datetime.timedelta(hours=1)
        return due.isoformat(timespec="seconds")
    if word.startswith("明天"):
        due = datetime.datetime.combine(
            now.date() + datetime.timedelta(days=1), datetime.time(14, 0)
        )
        return due.isoformat(timespec="seconds")
    # 这两天（parse 端已过滤非法档；未匹配一律按最宽档处理）
    return (now + datetime.timedelta(hours=48)).isoformat(timespec="seconds")


def commitment_status(entry: Dict[str, Any]) -> str:
    """账目状态：显式 ``status`` 优先，缺键 = pending（schema 表口径单一实现）。"""
    return str(entry.get("status") or "") or "pending"


def pending_commitments(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """读取账本里的全部 pending 条目（缺键 = 空账本，旧 state 零迁移）。"""
    focus = state.get("state", {}).get("focus", {})
    return [
        entry
        for entry in list(focus.get("commitments", []))
        if commitment_status(entry) == "pending"
    ]


def register_commitment(
    deps: Any,
    state: Dict[str, Any],
    *,
    now: datetime.datetime,
    who: str,
    what: str,
    when: str,
) -> bool:
    """登记一条 commitment 到 ``focus.commitments`` 并落账。

    - due 按档位分档（``resolve_due``）；
    - pending 数超 ``[intent].pending_cap`` → **最老**的 pending 条目转 expired
      （staleness 淘汰：``verdict=stale``——不静默丢，指标可查；不写编年史，
      淘汰是容量行为不是叙事事件）；
    - 返回是否登记成功（输入退化 = False，静默放弃）。
    """
    who = str(who or "").strip()
    what = str(what or "").strip()
    if not who or not what:
        return False
    cfg = deps.config
    due = resolve_due(when, now, str(cfg.narrative.sleep_time or ""))
    entry = make_commitment_event(
        ts=now.isoformat(timespec="seconds"),
        text=f"一直惦记着{who}：{what}",
        what=what,
        target_ref=who,
        due_ts=due,
    )
    inner = state["state"]
    focus = inner.setdefault("focus", {})
    book = list(focus.get("commitments", []))
    pending = [e for e in book if commitment_status(e) == "pending"]
    cap = max(1, int(cfg.intent.pending_cap))
    overflow = len(pending) - cap + 1
    if overflow > 0:
        for old in pending[:overflow]:
            old["status"] = "expired"
            old["verdict"] = "stale"
    book.append(entry)
    focus["commitments"] = book
    deps.state.save_self_state(state)
    telemetry = getattr(deps, "telemetry", None)
    if telemetry is not None:
        telemetry.record("commitment_created", 1, scope="creation")
    return True
