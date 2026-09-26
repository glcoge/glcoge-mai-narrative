"""生活片段生成：创作层唯一的日常 LLM 节点（prompt 构造 + 闸门 + 落库）。

从 ``state/engine.py`` 拆出（v0.2.0 批 2-C7，执行路线「类拆分推迟表」）：
``maybe_generate_life_fragment`` / ``_life_fragment_tier`` / ``_build_life_fragment_prompt``。

依赖方向：本模块接受 ``engine`` 实例（不持有状态），engine 侧保留一层薄委托方法，
对外 API 不变——与 ``proactive/sourcing.py`` 同一约定。

⚠ 循环导入：``state/engine.py`` 会在模块顶层 import 本模块，故本模块**不得**在顶层
import engine。``_SELF_SCOPE`` / 档位常量 / ``minutes_until_clock`` 采用函数内延迟导入。
``render.audience`` 不 import engine，可以顶层 import。

🔒 **守卫出口**：产出落库前要过双向守卫的第一道（入库前，ADR-0002 §9）。
本模块是「创作产出」的唯一定义地，故该闸门挂在这里，不散到别处。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from ..render.audience import visible_events

#: 生活片段产出的 kind ∈ GENERAL_KINDS（render/audience.GENERAL_KINDS），
#: 默认不带 source_uid 故天然通用——写入侧约定见 audience.py 的模块文档。
_FRAGMENT_TIER_LENGTH = {
    "flat": "40~90",    # 分级关闭：旧行为
    "minor": "40~90",   # 无素材：纯状态切片
    "normal": "80~180",  # 有对话素材
    "major": "200~400",  # 命中重要度信号
}
# prompt 里每条素材的展示上限（字符）；入库时统一保留 _FRAGMENT_MATERIAL_STORE_CAP
_FRAGMENT_TIER_MATERIAL_CAP = {"flat": 80, "minor": 80, "normal": 120, "major": 300}
# 素材密度阈值：窗口内对话素材 ≥ 该条数即判 major
_FRAGMENT_MATERIAL_DENSITY = 4
# 状态极端阈值（energy）
_FRAGMENT_ENERGY_LOW = 0.25
_FRAGMENT_ENERGY_HIGH = 0.9


async def maybe_generate_life_fragment(engine: Any, now: Optional[datetime] = None) -> None:
    """创作层消费器：间隔 + 每日上限闸门下，用轻量模型生成一段"生活片段"。

    目的：让 bot 的"生活"不只是数字变化，而是有一段段可被对话引用的生活故事
    （修复主动消息复述问题的第一环）。生成结果双写：
    - ``focus.pending_events``（有界 5 条，渲染时引用为"最近的生活片段"）；
    - ``chronicle``（append-only，kind=life，日记侧可见）。

    只读事件队列，不消费（三个原因：编年史 23:30 也要读同一批素材；
    事件有每日上限本来就有界；3 天前的由 _dequeue_expired_branch_events 清理）。
    """
    cfg = engine._plugin.config
    if not cfg.plugin.enabled or not cfg.narrative.enabled:
        return
    if int(cfg.narrative.life_fragment_daily_max) <= 0:
        return

    current = now or engine._local_now()
    today = current.strftime("%Y-%m-%d")
    state = engine.load_self_state()
    routine = state["state"].setdefault("routine", {})
    day_count = engine._store.get_kv_int(f"life_fragment:count:{today}")

    # 睡眠闸门（v0.1.10）：睡着不生产生活片段。
    # 这是「日记里每天都有深夜还醒着」的根因修复——此前创作层只有间隔与日上限两道
    # 闸门，深夜照常生产，而 prompt 只说「你处于深夜」，模型自然就写出「深夜还醒着」。
    if str(routine.get("sleep_state", "awake")) == "asleep":
        return

    # 起床补一段（v0.1.10）：醒来那一刻强制写一段，豁免间隔闸门与日上限。
    # 理由：睡眠期间不生产，不补的话早上所有用户拿到的由头都会是昨晚睡前那一条。
    wake_fragment = bool(routine.pop("wake_fragment_pending", False)) and bool(
        cfg.narrative.wake_fragment_enabled
    )

    window_start = current - timedelta(minutes=int(cfg.narrative.life_fragment_interval_minutes))
    if not wake_fragment:
        # 每日次数上限
        if day_count >= int(cfg.narrative.life_fragment_daily_max):
            return

        # 间隔闸门：距上次生成不足 interval 则跳过（不调 LLM、零成本）
        last_ts = engine._store.get_kv_str("life_fragment:last_ts")
        if last_ts:
            try:
                last_dt = datetime.fromisoformat(last_ts)
                if (current - last_dt).total_seconds() < int(cfg.narrative.life_fragment_interval_minutes) * 60:
                    return
                window_start = last_dt
            except (TypeError, ValueError):
                pass

    # 收集素材：全部模式用户的支线事件（最近一条对话素材 → 生活的原料）
    materials: List[str] = []
    for user_id in (cfg.narrative.mode_user_ids or []):
        for item in visible_events(engine._store, f"branch:{user_id}", user_id, 20):
            source_text = str(item.get("bysource", "") or "").strip()
            if source_text:
                materials.append(source_text)

    # 详略档位：按事件重要度决定本次创作长度（flat=分级关闭，回旧行为）
    tier = life_fragment_tier(engine, current, state, window_start)
    from .chronicle import load_native_personality

    persona = await load_native_personality(engine)
    prompt = build_life_fragment_prompt(
        engine, current, state, materials, persona=persona, tier=tier, wake=wake_fragment
    )
    if cfg.llm.show_prompt:
        engine._plugin.ctx.logger.info("生活片段 prompt: %s", prompt[:300])

    text = await engine._creator.generate(prompt)
    if not text:
        return

    # 指标 4 双轨埋点（R24 A 轨 / R7 B 轨）：只在产出这一刻采样，
    # 保证"注入侧组合熵"与"产出侧文本多样性"同尺度可比。
    telemetry = getattr(engine._plugin, "_telemetry", None)
    if telemetry is not None:
        telemetry.note_fragment(state, text)

    # 双写：pending_events（有界）+ 编年史（append-only）
    inner = state["state"]
    focus = inner.setdefault("focus", {})
    pending = list(focus.get("pending_events", []))
    # tier 随片段落库：由头签发要按档位做质量门槛（minor 不单独作由头，见 build_bysource）
    pending.append(
        {
            "ts": current.isoformat(timespec="seconds"),
            "text": text,
            "tier": tier,
        }
    )
    focus["pending_events"] = pending[-5:]
    engine.save_self_state(state)
    # 生活片段照常生成（pending_events 是主动消息的由头来源，不能断），
    # 仅"写入编年史"这一步受 chronicle_enabled 约束（2026-09-16：
    # 此前该开关管不到 life，名不副实）
    if cfg.narrative.chronicle_enabled:
        from ..state.engine import _SELF_SCOPE

        engine._store.append_chronicle(
            _SELF_SCOPE, "life", text, current.isoformat(timespec="seconds")
        )

    # 闸门推进 + 计数（last_ts 直接存 ISO 字符串，不再用 dict 包装）
    # 起床补一段豁免日上限（它是状态转换的必然产物，不是可选的创作），
    # 但仍推进 last_ts——否则醒来后第一段正常片段会紧接着挤进来。
    engine._store.set_kv_str("life_fragment:last_ts", current.isoformat(timespec="seconds"))
    if not wake_fragment:
        engine._store.set_kv_int(f"life_fragment:count:{today}", day_count + 1)
    engine._plugin.ctx.logger.info(
        "生活片段已生成（今日 %s/%s，档位 %s%s）: %s",
        day_count if wake_fragment else day_count + 1,
        cfg.narrative.life_fragment_daily_max,
        tier,
        "，起床补一段" if wake_fragment else "",
        text[:40],
    )


def life_fragment_tier(
    engine: Any, now: datetime, state: Dict[str, Any], window_start: datetime
) -> str:
    """判定本次生活片段的详略档位（flat/minor/normal/major）。

    2026-09-21 定案的三信号组合（纯规则，零额外 LLM 调用）：
    ① 关系里程碑：窗口内支线 ``milestones`` 有新增（关系阶段晋升，强信号）；
    ② 状态极端：self 层 energy ≤ 低阈 或 ≥ 高阈；
    ③ 素材密度：窗口内对话素材条数 ≥ 阈值。

    任一命中 → major；无命中但有素材 → normal；无素材 → minor。
    ``[narrative].life_fragment_detail_enabled=false`` 时返回 flat（旧行为）。
    """
    cfg = engine._plugin.config
    if not cfg.narrative.life_fragment_detail_enabled:
        return "flat"

    energy = float(state["state"]["mood"].get("energy", 0.0))
    if energy <= _FRAGMENT_ENERGY_LOW or energy >= _FRAGMENT_ENERGY_HIGH:
        return "major"

    start_iso = window_start.isoformat(timespec="seconds")
    material_count = 0
    for user_id in (cfg.narrative.mode_user_ids or []):
        branch = engine.load_branch_state(user_id)
        # 里程碑是只读事实（批 2 迁到 relationship 命名空间）；作为素材密度
        # 信号保留——它是"这段时间确实发生了事"的证据，不是关系演化值
        for item in branch.get("relationship", {}).get("milestones") or []:
            if str(item.get("ts", "")) >= start_iso:
                return "major"
        for event in visible_events(engine._store, f"branch:{user_id}", user_id, 20):
            if str(event.get("ts", "")) >= start_iso and str(event.get("bysource", "")).strip():
                material_count += 1

    if material_count >= _FRAGMENT_MATERIAL_DENSITY:
        return "major"
    return "normal" if material_count else "minor"


def build_life_fragment_prompt(
    engine: Any,
    now: datetime,
    state: Dict[str, Any],
    materials: Sequence[str],
    persona: str = "",
    tier: str = "flat",
    wake: bool = False,
) -> str:
    """构造生活片段生成 prompt（档位决定长度与素材展示量）。

    Args:
        wake: 本次是「起床补一段」（v0.1.10），prompt 追加刚醒的语境。
    """
    from ..state.engine import minutes_until_clock

    cfg = engine._plugin.config
    identity = cfg.identity
    inner = state["state"]
    personality = (
        persona
        or (f"生活在{identity.world}的角色" if identity.world else "")
        or "一个角色"
    )
    chunks = [
        f"你是{personality}，正在度过自己的一天。",
        (
            f"此刻：{now.strftime('%Y-%m-%d %H:%M')}，"
            f"你处于{inner['routine']['phase']}，"
            f"心情{inner['mood']['label']}（精力 {inner['mood']['energy'] * 10:.0f}/10）。"
        ),
    ]
    # 起床补一段：这正是「醒来后的第一段生活片段」，不点明的话模型会写成白天的状态切片
    if wake:
        chunks.append("你刚睡醒不久——请写醒来后的这一段。")
    else:
        # 临近入睡：让当天最后一段自然收在「困了、要睡了」上（v0.1.10）
        to_sleep = minutes_until_clock(cfg.narrative.sleep_time, now)
        if to_sleep is not None and to_sleep <= int(cfg.narrative.sleep_pre_sleep_hint_minutes):
            chunks.append("你有点困了，准备睡了——这会是今天最后一段生活片段。")
    material_cap = _FRAGMENT_TIER_MATERIAL_CAP.get(tier, 80)
    if materials:
        shown = [text[:material_cap] for text in materials[-6:]]
        chunks.append("最近发生的对话与小事：\n- " + "\n- ".join(shown))

    length = _FRAGMENT_TIER_LENGTH.get(tier, "40~90")
    if tier == "major":
        chunks.append(
            f"请以第一人称写一段 {length} 字的生活片段，把这件事写细："
            "起因是什么、你当下的具体感受、眼前的细节画面，"
            "以及你因此悄悄变化的一点认知。要有生活气息和画面感，"
            "只输出正文，不要任何标题/引号/表情/动作旁白。"
        )
    else:
        chunks.append(
            f"请以第一人称写一段 {length} 字的生活片段：你此刻心里的一段念头、"
            "一件正在想的小事、一句生活里的感慨。要有生活气息和画面感，"
            "只输出正文，不要任何标题/引号/表情/动作旁白。"
        )
    return "\n".join(chunks)


__all__ = [
    "build_life_fragment_prompt",
    "life_fragment_tier",
    "maybe_generate_life_fragment",
]
