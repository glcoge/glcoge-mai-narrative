"""编年史压缩：每日睡前小结（唯一常规 LLM 节点的 prompt 构造与写库）。

从 ``state/engine.py`` 拆出（v0.2.0 批 2-C7，执行路线「类拆分推迟表」）：
``maybe_daily_chronicle`` / ``_build_chronicle_prompt`` / ``_load_native_personality``。

依赖方向：本模块接受 ``engine`` 实例（不持有状态），engine 侧保留一层薄委托方法，
对外 API 不变（``engine.maybe_daily_chronicle`` 等照旧）——与 ``proactive/sourcing.py``
同一约定（改可见性会连带 33+ 处测试绑定点，纯移动不该动它们）。

⚠ 循环导入：``state/engine.py`` 会在模块顶层 import 本模块，故本模块**不得**在顶层
import engine。``_SELF_SCOPE`` / ``parse_clock`` 采用函数内延迟导入。
``render.audience`` 不 import engine，可以顶层 import。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from ..render.audience import visible_events


async def load_native_personality(engine: Any) -> str:
    """读取主程序原生 [personality].personality（人设唯一来源，失败降级为空串）。"""
    try:
        return str(await engine._plugin.ctx.config.get("personality.personality", "") or "").strip()
    except Exception as exc:
        engine._plugin.ctx.logger.debug("读取原生 personality 失败: %s", exc)
        return ""


async def maybe_daily_chronicle(engine: Any, now: Optional[datetime] = None) -> None:
    """当日有互动时，用轻量模型生成一条"今日小结"写入编年史（kind=daily）。

    日期归属（2026-09-16 接线时修正）：目标日期 = **触发点所属的那一天**。
    旧写法是 ``current.time() < trigger → return``，但 tick 间隔 30 分钟，
    若错过 ``[trigger, 24:00)`` 窗口（典型：23:30 触发、tick 落在 00:00），
    判定就会一直不成立 → **功能永不执行**。故过了午夜要能补写昨天。
    """
    from ..state.engine import _SELF_SCOPE, parse_clock

    cfg = engine._plugin.config
    if not cfg.plugin.enabled or not cfg.narrative.enabled:
        return
    if not cfg.narrative.chronicle_enabled:
        return
    trigger = parse_clock(cfg.narrative.daily_chronicle_time)
    if trigger is None:
        return

    current = now or engine._local_now()
    state = engine.load_self_state()

    # 睡眠态启用时，等**真正入睡**才写：一天到入睡才算结束。
    # 旧行为是「过了 daily_chronicle_time 就写」，但入睡可能因仍在聊被推迟
    # （最多 sleep_delay_max_minutes），那样小结会漏掉入睡前的最后一段对话。
    if engine._sleep_configured() and str(
        state["state"].get("routine", {}).get("sleep_state", "awake")
    ) != "asleep":
        return

    target_date = current.date()
    if current.time() < trigger:
        target_date = target_date - timedelta(days=1)
    date_text = target_date.strftime("%Y-%m-%d")

    # 幂等统一走 store 的标准键。旧代码手写的 ``chronicle:done:{today}``
    # 与 is_chronicle_done 用的 ``chronicle:{scope}:{kind}:{date}`` 是两套并存。
    if engine._store.is_chronicle_done(_SELF_SCOPE, "daily", date_text):
        return

    if state["state"].get("last_talk_date") != date_text:
        return  # 那天没说过话，不写

    materials: List[str] = []
    for user_id in (cfg.narrative.mode_user_ids or []):
        # 受众过滤（ADR-0004）：此处 scope 已是 branch:{user_id}，当前等价于原行为；
        # 走过滤入口是为跨用户取材（C4）预留——届时涉私原文不会进创作 prompt。
        for item in visible_events(engine._store, f"branch:{user_id}", user_id, 50):
            if str(item.get("ts", "")).startswith(date_text):
                materials.append(str(item.get("bysource", "")))

    persona = await load_native_personality(engine)
    # prompt 里的日期取自入参 now，故传目标日期而非当前时刻
    target_dt = datetime.combine(target_date, trigger)
    prompt = build_chronicle_prompt(engine, target_dt, state, materials, persona=persona)
    if cfg.llm.show_prompt:
        engine._plugin.ctx.logger.info("编年史 prompt: %s", prompt[:300])

    text = await engine._creator.generate(prompt)
    if text:
        engine._store.append_chronicle(
            _SELF_SCOPE, "daily", text, target_dt.isoformat(timespec="seconds")
        )
        engine._plugin.ctx.logger.info("编年史今日小结已写入: %s", date_text)
    engine._store.mark_chronicle_done(_SELF_SCOPE, "daily", date_text)


def build_chronicle_prompt(
    engine: Any,
    now: datetime,
    state: Dict[str, Any],
    materials: Sequence[str],
    persona: str = "",
) -> str:
    """构造编年史压缩 prompt（40~90 字的一日小结，第一人称）。"""
    cfg = engine._plugin.config
    identity = cfg.identity
    inner = state["state"]
    personality = (
        persona
        or (f"生活在{identity.world}的角色" if identity.world else "")
        or "一个角色"
    )
    chunks = [
        f"你是{personality}，正在写今天（{now.strftime('%Y-%m-%d')}）的睡前日记。",
        f"今天的心情：{inner['mood']['label']}（精力 {inner['mood']['energy']:.2f}）。",
    ]
    if materials:
        chunks.append("今天发生过的事：\n- " + "\n- ".join(materials[-8:]))
    chunks.append(
        "请用第一人称写一段 40~90 字的今日小结，像睡前随手记录，"
        "只输出正文，不要任何标题/引号/表情。"
    )
    return "\n".join(chunks)


__all__ = [
    "build_chronicle_prompt",
    "load_native_personality",
    "maybe_daily_chronicle",
]
