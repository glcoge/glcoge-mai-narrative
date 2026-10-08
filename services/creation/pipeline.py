"""创作管道基元（深化 A / 架构体检候选 A）——两供给管道的**真重复**收口。

背景：life（生活片段）与 seeder（世界事件播种）两条创作管道 13 个骨架段中
12 段结构同构，但**闸门顺序与间隔键推进时机是真语义差异**（life=日上限→间隔、
成功后才推进 last_ts、守卫丢弃可重试；seeder=间隔→尝试即推进 last_try→概率→
日上限）——强统编排便 spec interface 膨胀到与实现同宽（浅模块）。故本模块只
收口三件**逐字级重复**，编排保留在两管道本地（差异表见
`.workbuddy/v0.3重构/A+D执行方案.md` §0.1）：

1. **闸门基元**：``interval_gate`` / ``daily_cap_gate``——读/解析/比较的单一
   实现；返回 bool（True=放行），调用方决定顺序与时序；
2. **守卫拒绝日志**：``log_guard_reject``——「WARN 必须带原文」（R30 教训：
   守卫是子串匹配，只看命中片段无法分辨真违规与误伤）的统一落点；
   参数化 label/detail/ellipsis 使两管道文案**逐字节不变**；
3. **落账尾段**：``commit_created_event``——pending_events LRU 截断 +
   save_self_state + chronicle_enabled 条件写；``_SELF_SCOPE`` 延迟导入收进
   本模块一处（life/seeder 两处「避开循环」注释随之消失）。

🔴 红线④：高光签（``_draw_highlight`` / highlight 计数）**不进本模块**——
播种器与高光签两个物种互不触碰的物理隔离继续成立（P19 冻结区）。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

__all__ = [
    "interval_gate",
    "daily_cap_gate",
    "log_guard_reject",
    "commit_created_event",
]


def interval_gate(store: Any, last_key: str, now: datetime.datetime, *, minutes: int) -> bool:
    """间隔闸基元：距上次记录不足 ``minutes`` 分钟 → False（拦截）。

    语义与两管道现行实现逐项一致：无记录放行；空串放行；时间戳解析失败放行
    （fail-open——闸门只挡「确认太近」，不因脏数据卡死创作）；恰好等于
    ``minutes`` 放行（``<`` 严格比较）。分钟数不做 max(1,·) 防御——seeder
    的 ``max(1,·)`` 在其编排侧保留（life 无此防御，行为各按现状）。
    """
    last_raw = store.get_kv_str(last_key)
    if not last_raw:
        return True
    try:
        last_dt = datetime.datetime.fromisoformat(last_raw)
    except (TypeError, ValueError):
        return True
    return (now - last_dt).total_seconds() >= int(minutes) * 60


def daily_cap_gate(store: Any, count_key: str, *, cap: int) -> bool:
    """日上限闸基元：当日计数已达 ``cap`` → False（拦截）。

    ``cap`` 做 ``max(1, ·)`` 防御（0 无意义——与 seeder 现行防御一致；
    life 的 ``daily_max<=0`` 在编排侧更早 return，到达闸门时 cap 本已 >=1）。
    """
    return store.get_kv_int(count_key) < max(1, int(cap))


def log_guard_reject(
    logger: Any,
    *,
    label: str,
    detail: str = "",
    text: str,
    limit: int = 60,
    ellipsis: bool = True,
) -> None:
    """守卫拒绝的统一 WARN（原文必须带，R30）。

    文案模板：``{label}→ 已丢弃，不入库[；{detail}]；原文: {截断原文}``——
    detail 空则省略该段；``ellipsis=False`` 时超长原文裸截断（seeder 现行
    格式），默认带「…」（life 现行格式）。两管道调用各自传参，文案逐字节不变。
    """
    shown = (text[:limit] + "…") if (ellipsis and len(text) > limit) else text[:limit]
    message = f"{label}→ 已丢弃，不入库"
    if detail:
        message += f"；{detail}"
    logger.warning(f"{message}；原文: {shown}")


def commit_created_event(
    engine: Any,
    state: Dict[str, Any],
    *,
    entry: Dict[str, Any],
    chronicle_kind: str,
    now: datetime.datetime,
) -> None:
    """落账尾段：pending_events LRU 截断 + save + 编年史条件写（两管道逐字重复块）。

    - ``entry`` 由调用方构造（``make_fragment_event`` / ``make_seed_event``）；
    - LRU 容量 = ``[narrative].fragment_pending_max``（``max(1,·)`` 防 0——
      ``pending[-0:]`` 是「全量」不是「空」，config 侧已 ge=1，此处兜住测试
      夹具绕过校验的情况）；
    - 编年史受 ``chronicle_enabled`` 约束（2026-09-16：创作照常、仅写入步骤
      受开关管——pending_events 是由头来源，不能断）。
    """
    cfg = engine._plugin.config
    inner = state["state"]
    focus = inner.setdefault("focus", {})
    pending = list(focus.get("pending_events", []))
    pending.append(entry)
    focus["pending_events"] = pending[-max(1, int(cfg.narrative.fragment_pending_max)):]
    engine.save_self_state(state)

    if cfg.narrative.chronicle_enabled:
        from ..state.engine import _SELF_SCOPE  # 延迟导入：避开 engine ↔ pipeline 循环

        engine._store.append_chronicle(
            _SELF_SCOPE,
            chronicle_kind,
            str(entry.get("text", "") or ""),
            now.isoformat(timespec="seconds"),
        )
