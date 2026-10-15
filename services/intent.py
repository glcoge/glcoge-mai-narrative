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
    "build_recheck_prompt",
    "parse_verdict",
    "settle_due",
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


# ─── 到期重判（ADR-0006 红线 3：到期 ≠ 执行，重判先于一切动作）──────────

#: 重判判定的合法词表（prompt 输出契约与解析的唯一来源）
_VERDICTS = ("act", "wait", "fulfilled", "cancel")


def build_recheck_prompt(entry: Dict[str, Any], facts: str, now: datetime.datetime) -> str:
    """构造到期重判 prompt（内部裁决：输出不进任何注入块，红线 2 的逆命题）。

    三问对齐 2026-10-14 催饭案的三个缺口：对方做没做（宿主短期窗口截断）、
    事还成不成立（素材过时）、是不是关于这个人（弱绑定归属）。
    """
    facts_text = facts.strip() or "（没有检索到相关对话记录。）"
    return (
        "你在帮角色管理她的意图账本（内部裁决，输出不会给任何用户看）。\n"
        f"她在 {entry.get('ts', '')} 记下一条对人的挂念：\n"
        f"- 对象：{entry.get('target_ref', '')}\n"
        f"- 挂念的事：{entry.get('what', '')}\n"
        f"现在（{now.strftime('%Y-%m-%d %H:%M')}）到了当初设想的跟进时间。"
        "以下是那之后与这个对象有关的对话记忆检索结果：\n"
        f"{facts_text}\n"
        "请依次判断：\n"
        "1. 检索结果是否表明这件事对方已经做了、或已经回应过"
        "（包括角色自己已经问过/提过）？\n"
        "2. 这件挂念现在还成立吗，还是已经过时（时间已过/情况已变）？\n"
        "3. 它确实是关于这个对象的事吗？\n"
        "最后单独一行输出判定（四选一，格式严格）：\n"
        "[判定] act       还成立、对方还没回应、现在适合自然地跟进\n"
        "[判定] wait      还成立但现在不合适/拿不准/可能不是关于这个对象\n"
        "[判定] fulfilled 对方已经做了或已经回应过（或角色已经问过），不必再惦记\n"
        "[判定] cancel    已过时或不再成立\n"
        "判定行之前可以用一两句话说明理由。"
    )


def parse_verdict(raw: str) -> str:
    """从重判输出解析判定词；取**最后一次** `[判定]`，缺/非法一律 wait（保守兜底）。"""
    text = str(raw or "")
    idx = text.rfind("[判定]")
    if idx < 0:
        return "wait"
    tail = text[idx + len("[判定]"):].strip()
    for verdict in _VERDICTS:
        if tail.startswith(verdict):
            return verdict
    return "wait"


async def _retrieve_facts(plugin: Any, user_id: str, entry: Dict[str, Any]) -> Optional[str]:
    """裁决点检索（ADR-0006）：knowledge.search 按 what 查该用户在挂念登记之后的事实。

    返回 None = 基础设施失败（能力被拒/异常）→ 调用方推迟不裁（宁晚勿盲催）；
    返回空串 = 检索成功但没有相关记录（合法空，重判照常进行）。
    """
    try:
        result = await plugin.ctx.call_capability(
            "knowledge.search",
            query=str(entry.get("what", "") or "")[:80],
            limit=5,
            user_id=str(user_id or ""),
            time_start=str(entry.get("ts", "") or ""),
        )
    except Exception as exc:  # noqa: BLE001 —— 检索失败不得拖垮主动开口主链路
        plugin.ctx.logger.warning("意图重判检索失败（本轮推迟不裁）: %s", exc)
        return None
    if not isinstance(result, dict) or result.get("success") is not True:
        plugin.ctx.logger.warning("意图重判检索被拒: %r", result)
        return None
    content = str(result.get("content", "") or "").strip()
    if "你不太了解" in content:
        # 宿主空结果文案（capabilities/data.py），归一化为合法空
        content = ""
    return content


async def settle_due(plugin: Any, user_id: str, now: datetime.datetime) -> Optional[Dict[str, str]]:
    """到期重判入口（``scheduler._fire`` 签发普通由头**之前**调用）。

    每轮至多结算**一条**（最老到期者）；四态裁决（ADR-0006）：
    - ``act``       → 返回由头 detail（origin=commitment），本轮由头改用它；
    - ``wait``      → due 后移 ``[intent].wait_defer_hours``，返回 None；
    - ``fulfilled`` → status=resolved + 指标，返回 None（正常走普通素材）；
    - ``cancel``    → status=expired + 指标 + 编年史留痕一条，返回 None。

    附带两道零 LLM 卫生闸：staleness 自动作废（due 后 ``stale_expire_hours``
    仍未消化）与重判冷却（``recheck_cooldown_minutes`` 内不重复裁决）。
    检索基础设施失败 → 按 wait 推迟（宁晚勿盲催，2026-10-15 裁定）。
    """
    deps = plugin._engine.deps
    cfg = plugin.config
    state = deps.state.load_self_state()
    focus = state["state"].setdefault("focus", {})
    book = list(focus.get("commitments", []))
    if not book:
        return None

    changed = False
    stale_cut = now - datetime.timedelta(hours=max(1, int(cfg.intent.stale_expire_hours)))
    for entry in book:
        if commitment_status(entry) != "pending":
            continue
        try:
            due = datetime.datetime.fromisoformat(str(entry.get("due_ts", "") or ""))
        except (TypeError, ValueError):
            continue
        if due <= stale_cut:
            entry["status"] = "expired"
            entry["verdict"] = "stale"
            entry["resolved_ts"] = now.isoformat(timespec="seconds")
            changed = True
            telemetry = getattr(plugin, "_telemetry", None)
            if telemetry is not None:
                telemetry.record("commitment_resolved", 1, user_id=user_id, scope="stale")

    cooldown = datetime.timedelta(minutes=max(1, int(cfg.intent.recheck_cooldown_minutes)))
    candidate = None
    pending_sorted = sorted(
        (e for e in book if commitment_status(e) == "pending"),
        key=lambda e: str(e.get("due_ts", "")),
    )
    for entry in pending_sorted:
        try:
            due = datetime.datetime.fromisoformat(str(entry.get("due_ts", "") or ""))
        except (TypeError, ValueError):
            continue
        if due > now:
            continue
        last_raw = str(entry.get("last_check_ts", "") or "")
        if last_raw:
            try:
                if now - datetime.datetime.fromisoformat(last_raw) < cooldown:
                    continue
            except (TypeError, ValueError):
                pass  # 坏时间戳放行（fail-open，同 interval_gate 口径）
        candidate = entry
        break
    if changed:
        deps.state.save_self_state(state)
    if candidate is None:
        return None

    candidate["last_check_ts"] = now.isoformat(timespec="seconds")
    facts = await _retrieve_facts(plugin, user_id, candidate)
    if facts is None:
        # 检索基础设施失败：宁晚勿盲催（2026-10-15 裁定）——推迟并留痕
        candidate["due_ts"] = (
            now + datetime.timedelta(hours=max(1, int(cfg.intent.wait_defer_hours)))
        ).isoformat(timespec="seconds")
        deps.state.save_self_state(state)
        telemetry = getattr(plugin, "_telemetry", None)
        if telemetry is not None:
            telemetry.record("commitment_rechecked", 1, user_id=user_id, scope="retrieve_failed")
        return None

    prompt = build_recheck_prompt(candidate, facts, now)
    raw = await deps.creator.generate(prompt)
    verdict = parse_verdict(raw)
    telemetry = getattr(plugin, "_telemetry", None)
    if telemetry is not None:
        telemetry.record("commitment_rechecked", 1, user_id=user_id, scope=verdict)

    defer = (
        now + datetime.timedelta(hours=max(1, int(cfg.intent.wait_defer_hours)))
    ).isoformat(timespec="seconds")
    if verdict == "act":
        # 防复读：act 后跟进时点同步后移——下一冷却圈的重判检索会看到「已问过」，
        # 自然落 fulfilled，不会原样连发
        candidate["due_ts"] = defer
        deps.state.save_self_state(state)
        return {
            "text": str(candidate.get("text", "") or ""),
            "event_id": str(candidate.get("event_id", "") or ""),
            "origin": "commitment",
        }
    if verdict == "wait":
        candidate["due_ts"] = defer
        deps.state.save_self_state(state)
        return None
    if verdict == "fulfilled":
        candidate["status"] = "resolved"
        candidate["verdict"] = "fulfilled"
        candidate["resolved_ts"] = now.isoformat(timespec="seconds")
        deps.state.save_self_state(state)
        if telemetry is not None:
            telemetry.record("commitment_resolved", 1, user_id=user_id, scope="fulfilled")
        return None

    # cancel：作废 + 编年史留痕（红线 4：不静默丢弃）。kind=commitment 不在
    # 注入/证据白名单内，纯审计痕（AUDIT 用途见 ADR-0006）。
    candidate["status"] = "expired"
    candidate["verdict"] = "cancel"
    candidate["resolved_ts"] = now.isoformat(timespec="seconds")
    deps.state.save_self_state(state)
    if telemetry is not None:
        telemetry.record("commitment_resolved", 1, user_id=user_id, scope="cancel")
    from .state.engine import _SELF_SCOPE  # 延迟导入：同 pipeline 先例，避循环

    deps.store.append_chronicle(
        _SELF_SCOPE,
        "commitment",
        f"不再惦记{candidate.get('target_ref', '')}的{candidate.get('what', '')}了"
        f"（到期裁决：作废，{now.strftime('%m-%d %H:%M')}）",
        now.isoformat(timespec="seconds"),
    )
    return None
