"""晋升健康度诊断（**只读**，不写任何状态）。

为什么需要它
------------
晋升机是确定性状态机，门槛在**归档数据**上定标（批 4 闸门：22 天 → 8 天命中）。
上线后如果它悄悄不再产出，外表与「正常但在等时机」**完全一样**——等到几周后
发现时，已经攒了好几周的沉默。这正是 HDSI 5.9「恒空」的原样重演：机制建完、
闸门过了、没人盯，静默失效。

本模块把「在等时机」与「在空转」区分开，回答四件事：

1. 距上次真实晋升多久（``last_promotion`` / ``age_days`` / ``stale``）
2. 哪些慢变路径正被冷却锁住、什么时候解禁（``cooldowns``）
3. 每条待决提案卡在门槛的哪一项（``pending[*].failed``）
4. 证据池现在有多厚（``evidence_pool``）

纪律
----
- **只读**：不写 kv、不写表、不改状态。
- **判据同源**：门槛复用 ``continuity.minor_gate_checks``，冷却复用
  ``continuity.is_after_cooldown``，证据池复用 ``proposal.collect_evidence``。
  本模块**不写第二份判据**——诊断一旦漂移，它就会开始描述一个与晋升机不同的世界。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional

from ..state.continuity import (
    is_after_cooldown,
    minor_gate_checks,
    parse_chronicle_refs,
    scene_stats,
)
from .proposal import collect_evidence

#: 冷却键前缀（与 ``PromotionEngine._cooldown_key`` 同格式；跨重启存在 kv 里）
COOLDOWN_PREFIX = "promotion:cooldown:"

#: 「距上次晋升超过这么多天」即判为可疑空转（默认 14 天 ≈ 两个冷却周期）
STALE_DAYS = 14.0


def _field(row: Any, key: str, default: str = "") -> str:
    """读取行字段（dict 或属性都可，判据层用，不吞异常）。"""
    value = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    return str(value) if value is not None else default


def _cooldown_state(
    store: Any, path: str, *, now: datetime.datetime, cooldown_hours: float
) -> Dict[str, Any]:
    """读一条路径的冷却状态（解禁时刻 / 剩余小时 / 是否锁住）。"""
    last_ts = store.get_kv_str(f"{COOLDOWN_PREFIX}{path}", "")
    blocked = not is_after_cooldown(last_ts, now=now, cooldown_hours=cooldown_hours)
    hours_left = 0.0
    if blocked and last_ts:
        try:
            last = datetime.datetime.fromisoformat(last_ts.strip())
        except ValueError:
            last = None
        if last is not None:
            hours_left = max(
                0.0, (float(cooldown_hours) - (now - last).total_seconds() / 3600.0)
            )
            release = last + datetime.timedelta(hours=float(cooldown_hours))
            return {
                "path": path,
                "last_ts": last_ts,
                "release_ts": release.isoformat(timespec="seconds"),
                "hours_left": round(hours_left, 2),
                "blocked": True,
            }
    return {
        "path": path,
        "last_ts": last_ts,
        "release_ts": last_ts,
        "hours_left": 0.0,
        "blocked": blocked,
    }


def promotion_health(
    store: Any,
    config: Any,
    *,
    now: datetime.datetime,
    stale_days: float = STALE_DAYS,
) -> Dict[str, Any]:
    """盘诊晋升机：**在等时机**还是**在空转**。

    Args:
        store: ``NarrativeStore``（只读访问）。
        config: ``[promotion]`` 配置段（门槛值的单一来源）。
        now: 当前时刻（由调用方注入，便于测试与回放）。
        stale_days: 距上次晋升超过这么多天即判为可疑空转。

    Returns:
        结构化盘诊结果，见模块 docstring 的四件事。
    """
    cooldown_hours = float(getattr(config, "cooldown_hours", 0) or 0)
    gate = {
        "minor_confidence": float(getattr(config, "minor_confidence", 0) or 0),
        "minor_min_scenes": int(getattr(config, "minor_min_scenes", 0) or 0),
        "minor_min_days": int(getattr(config, "minor_min_days", 0) or 0),
        "cooldown_hours": cooldown_hours,
        "major_enabled": bool(getattr(config, "major_enabled", False)),
    }

    # ① 上次真实晋升（留痕表最新一条；新→旧）
    history = store.list_promotions(limit=1) or []
    last_promotion: Optional[Dict[str, Any]] = None
    age_days: Optional[float] = None
    if history:
        row = history[0]
        ts = _field(row, "ts")
        last_promotion = {
            "ts": ts,
            "path": _field(row, "path"),
            "reason": _field(row, "reason"),
            "action": _field(row, "action"),
        }
        try:
            last = datetime.datetime.fromisoformat(ts.strip())
        except ValueError:
            last = None
        if last is not None:
            age_days = round((now - last).total_seconds() / 86400.0, 2)

    # ② 证据池：提案提炼真正能看到的材料（复用 collect_evidence，不另写掩码）
    pool_rows = collect_evidence(store)
    scenes, days = scene_stats(pool_rows)
    evidence_pool = {
        "entries": len(pool_rows),
        "scenes": scenes,
        "days": days,
    }

    # ③ 待决提案逐条归因
    pending_rows = store.list_proposals(status="pending", limit=200) or []
    pending: List[Dict[str, Any]] = []
    for row in pending_rows:
        path = _field(row, "path")
        confidence = float(row.get("confidence") or 0.0)
        refs = parse_chronicle_refs(_field(row, "evidence_refs"))
        ev_scenes, ev_days = scene_stats(store.list_chronicle_by_ids(refs))
        cooling = not is_after_cooldown(
            store.get_kv_str(f"{COOLDOWN_PREFIX}{path}", ""),
            now=now,
            cooldown_hours=cooldown_hours,
        )
        checks = minor_gate_checks(
            confidence=confidence,
            scene_count=ev_scenes,
            day_count=ev_days,
            min_confidence=gate["minor_confidence"],
            min_scenes=gate["minor_min_scenes"],
            min_days=gate["minor_min_days"],
        )
        failed = [name for name, ok in checks.items() if not ok]
        pending.append(
            {
                "id": row.get("id"),
                "path": path,
                "confidence": confidence,
                "scene_count": ev_scenes,
                "day_count": ev_days,
                "checks": dict(checks),
                "failed": failed,
                "blocking": "cooldown" if cooling else ("gate" if failed else None),
            }
        )

    # ③-b 冷却总览：历史晋升过的路径 + 当前待决路径，去重
    paths = [
        _field(row, "path") for row in (store.list_promotions(limit=200) or [])
    ] + [item["path"] for item in pending]
    cooldowns: List[Dict[str, Any]] = []
    for path in dict.fromkeys(p for p in paths if p):
        cooldowns.append(
            _cooldown_state(store, path, now=now, cooldown_hours=cooldown_hours)
        )

    # ④ 结论
    if any(item["blocking"] is None for item in pending):
        verdict = "ready"
    elif not pending:
        verdict = "no_proposals"
    elif all(item["blocking"] == "cooldown" for item in pending):
        verdict = "cooling"
    else:
        verdict = "below_gate"

    return {
        "now": now.isoformat(timespec="seconds"),
        "verdict": verdict,
        "gate": gate,
        "last_promotion": last_promotion,
        "age_days": age_days,
        "stale": bool(age_days is not None and age_days > float(stale_days)),
        "cooldowns": cooldowns,
        "pending": pending,
        "evidence_pool": evidence_pool,
    }


__all__ = [
    "COOLDOWN_PREFIX",
    "STALE_DAYS",
    "promotion_health",
]
