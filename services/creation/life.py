"""生活片段生成：创作层唯一的日常 LLM 节点（prompt 构造 + 闸门 + 落库）。

从 ``state/engine.py`` 拆出（v0.2.0 批 2-C7，执行路线「类拆分推迟表」）：
``maybe_generate_life_fragment`` / ``_draw_highlight`` / ``_build_life_fragment_prompt``。

依赖方向：本模块接受 ``engine`` 实例（不持有状态），engine 侧保留一层薄委托方法，
对外 API 不变——与 ``proactive/sourcing.py`` 同一约定。

⚠ 循环导入：``state/engine.py`` 会在模块顶层 import 本模块，故本模块**不得**在顶层
import engine。``_SELF_SCOPE`` / 档位常量 / ``minutes_until_clock`` 采用函数内延迟导入。
``render.audience`` 不 import engine，可以顶层 import。

🔒 **守卫出口**：产出落库前要过双向守卫的第一道（入库前，ADR-0002 §9）。
本模块是「创作产出」的唯一定义地，故该闸门挂在这里，不散到别处。
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..render.audience import visible_events
from ..learning.topic import TOPIC_WEIGHT_SELF_FRAGMENT, reward_topic
from ..state.continuity import build_guard_keywords, guard_violations, should_drop_output
from .event_entity import make_fragment_event

#: 通信事实标记（批 4-C9 / R25 / ADR-0003 §8；E8 裁定 (b)：**只做合约句**，
#: 注入真实发送清单需接宿主账本，留收尾里程碑）。
#:
#: 为什么必须有这句：生活片段是**私下独白**，模型很容易把"想联系谁"顺手写成
#: 「已经发了消息」；下一轮模型又把这个片段当事实引用（"就我刚才说的"），
#: 而对方毫无记忆 → 对话自相矛盾。故明文约定：**通信事实以宿主账本为准**，
#: 片段里提到联系一律只写**意图**。
# OBSERVE(R25)：通信事实合约句——生活片段里「联系别人」只能写打算或念头，不得写成既成事实；
# 删掉即模型把「想回他一句」写成「已经发了」，下一轮自相矛盾。
# ⚠️ 本项只完成**合约句**：注入侧「最近真实已发送清单」仍挂起未做（需接宿主账本），
# 该边界由 pytests/test_comm_fact.py 钉住——三件套完成不等于本项完成。
COMM_FACT_RULE = (
    "通信事实以真实记录为准：如果片段里提到联系别人（发消息、打电话、说过什么话），"
    "只能写成你的**打算或心里的念头**（例如「想回他一句」「明天问问她」），"
    "不要写成「已经发了 / 已经说了」这类既成事实——你还没真的发出。"
)

#: 生活片段产出的 kind ∈ GENERAL_KINDS（render/audience.GENERAL_KINDS），
#: 默认不带 source_uid 故天然通用——写入侧约定见 audience.py 的模块文档。
#: 2026-09-30（方案 §5）：原 OBSERVE(R22)/OBSERVE(P1) 的 tier 三信号与长度区间表已整体删除，
#: 详略改由「高光签」驱动（事前抽签注入方差），R22/P1 迁《废弃接口登记表》。

#: prompt 里每条素材的展示上限统一 120 字符（§15-1 裁决，与档位无关）；
#: 入库时另由 engine 侧 _FRAGMENT_MATERIAL_STORE_CAP = 300 统一截断。
_FRAGMENT_MATERIAL_CAP = 120
#: 每日高光片段上限（P19，观察期冻结）：达到后当日不再抽签
_HIGHLIGHT_DAILY_MAX = 2
#: 高光签 RNG：模块级实例，单测 monkeypatch ``life._rng.random`` 即可钉住（§15-2 / HDSI 教训）
_rng = random.Random()


async def maybe_generate_life_fragment(engine: Any, now: Optional[datetime] = None) -> None:
    """创作层消费器：间隔 + 每日上限闸门下，用轻量模型生成一段"生活片段"。

    目的：让 bot 的"生活"不只是数字变化，而是有一段段可被对话引用的生活故事
    （修复主动消息复述问题的第一环）。生成结果双写：
    - ``focus.pending_events``（有界 ``fragment_pending_max`` 条，默认 12，渲染时引用为"最近的生活片段"）；
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
            except (TypeError, ValueError):
                pass

    # 收集素材：全部模式用户的支线事件（最近一条对话素材 → 生活的原料）
    materials: List[str] = []
    for user_id in (cfg.narrative.mode_user_ids or []):
        for item in visible_events(engine._store, f"branch:{user_id}", user_id, 20):
            source_text = str(item.get("bysource", "") or "").strip()
            if source_text:
                materials.append(source_text)

    # 高光签（Q5/Q8 / P19）：事前抽签注入详略方差，取代原 tier 三信号
    highlight = _draw_highlight(engine, today, wake=wake_fragment)
    from .chronicle import load_native_personality

    persona = await load_native_personality(engine)
    prompt = build_life_fragment_prompt(
        engine, current, state, materials, persona=persona, highlight=highlight, wake=wake_fragment
    )
    if cfg.llm.show_prompt:
        engine._plugin.ctx.logger.info("生活片段 prompt: %s", prompt[:300])

    text = await engine._creator.generate(prompt)
    if not text:
        return

    # 双向守卫**第一道**（入库前，ADR-0002 §9，批 2-C6）：
    # 命中的产出**整条丢弃**（不入 pending_events、不入编年史、不推进闸门计数）。
    # 不改写——改写等于让代码替模型撒谎，且改写产物没经过任何审查。
    # 此处是"创作产出"的唯一定义地，故闸门挂在这里，不散到别处（E9 裁决）。
    # 丢弃后**直接 return**，不推进 last_ts：这次尝试没有产出可用内容，
    # 不该占用配额；下一个 tick 会以同一批素材再试一次。
    guard_set = build_guard_keywords(cfg)
    if should_drop_output(text, guard_set):
        # OBSERVE(R30)：日志**必须带原文**——守卫是子串包含匹配，关键词又是从
        # world_rules 切出来的短碎片（如「尾巴」「角」），只看命中片段无法分辨
        # 「真违规」与「误伤」（真机 2026-09-27 16:02 就丢过一条只因出现「尾巴」）。
        # 没有原文就只能事后猜，这正是误伤率长期无法收敛的原因。
        engine._plugin.ctx.logger.warning(
            "生活片段命中锚定守卫（world_rules/values/禁用片段）→ 已丢弃，不入库；"
            "命中片段: %s；原文: %s",
            guard_violations(text, guard_set)[:5],
            (text[:60] + "…") if len(text) > 60 else text,
        )
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
    # highlight 随片段落库（Q14）：true 则 chronicle kind=life_highlight（进晋升证据池），
    # 且由头端给 2 倍权重（占槽实现，见 sourcing.build_bysource）。
    # 批 0（R41）：条目升级为事件实体（event_id + kind=fragment）——构造单一入口
    # 在 creation/event_entity.py；text/ts/highlight 原语义不变，读取端零迁移。
    pending.append(
        make_fragment_event(
            ts=current.isoformat(timespec="seconds"),
            text=text,
            highlight=highlight,
        )
    )
    # 容量取自配置（方案 §7 / P20）：max(1,·) 防 0——pending[-0:] 是「全量」不是
    # 「空」，容量语义下 0 无意义（config 侧已 ge=1，此处兜住测试夹具绕过校验的情况）。
    focus["pending_events"] = pending[-max(1, int(cfg.narrative.fragment_pending_max)):]
    engine.save_self_state(state)
    # 生活片段照常生成（pending_events 是主动消息的由头来源，不能断），
    # 仅"写入编年史"这一步受 chronicle_enabled 约束（2026-09-16：
    # 此前该开关管不到 life，名不副实）
    if cfg.narrative.chronicle_enabled:
        from ..state.engine import _SELF_SCOPE

        engine._store.append_chronicle(
            _SELF_SCOPE,
            "life_highlight" if highlight else "life",
            text,
            current.isoformat(timespec="seconds"),
        )

    # 闸门推进 + 计数（last_ts 直接存 ISO 字符串，不再用 dict 包装）
    # 起床补一段豁免日上限（它是状态转换的必然产物，不是可选的创作），
    # 但仍推进 last_ts——否则醒来后第一段正常片段会紧接着挤进来。
    engine._store.set_kv_str("life_fragment:last_ts", current.isoformat(timespec="seconds"))
    if not wake_fragment:
        engine._store.set_kv_int(f"life_fragment:count:{today}", day_count + 1)
    # 高光计数（P19）：只在本段成功落库后推进（守卫丢弃在上方已 return，不占配额）
    if highlight:
        engine._store.set_kv_int(
            f"life_fragment:highlight:count:{today}",
            engine._store.get_kv_int(f"life_fragment:highlight:count:{today}") + 1,
        )
    # 话题归因（批 3-C5 / R16，ADR-0003 §7）：片段自身主题**降权**累积，
    # 防「她写猫 → 素材全猫 → 对人人讲猫」的自主信息茧房
    reward_topic(engine._store, text, TOPIC_WEIGHT_SELF_FRAGMENT)
    engine._plugin.ctx.logger.info(
        "生活片段已生成（今日 %s/%s%s%s）: %s",
        day_count if wake_fragment else day_count + 1,
        cfg.narrative.life_fragment_daily_max,
        "，高光" if highlight else "",
        "，起床补一段" if wake_fragment else "",
        text[:40],
    )


def _draw_highlight(engine: Any, today: str, *, wake: bool) -> bool:
    """高光签（Q5/Q8 / P19）：事前抽签注入详略方差，取代原 tier 三信号。

    三档判定改为**二元抽签**——不再试图从外部环境推测「这段值不值得写细」，
    而是以固定概率主动注入方差（详略是结果，不是控制变量，HDSI 教训）。

    三条豁免/上限（方案 §5.1）：
    - 起床片段（``wake=True``）豁免：它是状态转换的必然产物，与「刚醒」语境冲突；
    - 总开关关闭 → 永不抽签（唯一降级开关，Q8）；
    - 当日已抽中数 ≥ ``_HIGHLIGHT_DAILY_MAX`` → 不再抽签（防「一天全是高光」）。
    """
    cfg = engine._plugin.config
    if wake or not cfg.narrative.life_fragment_detail_enabled:
        return False
    if engine._store.get_kv_int(f"life_fragment:highlight:count:{today}") >= _HIGHLIGHT_DAILY_MAX:
        return False
    return _rng.random() < float(cfg.narrative.highlight_probability)


def build_life_fragment_prompt(
    engine: Any,
    now: datetime,
    state: Dict[str, Any],
    materials: Sequence[str],
    persona: str = "",
    highlight: bool = False,
    wake: bool = False,
) -> str:
    """构造生活片段生成 prompt（高光与否决定是否追加四段式要求）。

    Args:
        highlight: 本段是否抽中高光签（Q5/Q8）——抽中则追加「写一件不寻常的小事」四段式。
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
    material_cap = _FRAGMENT_MATERIAL_CAP
    if materials:
        shown = [text[:material_cap] for text in materials[-6:]]
        chunks.append("最近发生的对话与小事：\n- " + "\n- ".join(shown))

    # 通信事实标记（C9）：所有片段都适用，故放在输出指令之前
    chunks.append(COMM_FACT_RULE)

    # 基础句（Q3）：**零数字**——篇幅是「真实发生过多少事」的结果，不是控制变量
    # （HDSI 教训：固定字数区间是上游实测失败过的同族方案，如「800 字上限」腰斩 /
    #  「400-700 字」填充事故。）
    chunks.append(
        "请以第一人称写下此刻的一段生活片段：可以是心里的一段念头、"
        "一件正在想的小事、一句生活里的感慨。篇幅跟着真实发生的事走——"
        "只有一两句想头就写得短些，事情多、心里转了几道弯自然会长些。"
        "要有生活气息和画面感，只输出正文，不要任何标题/引号/表情/动作旁白。"
    )
    # 高光追加句（Q2/Q5）：抽中时接在基础句之后，要求挑一件不寻常的小事写细（四段式）
    if highlight:
        chunks.append(
            "今天这一段请挑一件不太寻常的小事来写：起因是什么、你当下的具体感受、"
            "眼前停得最久的那个画面，以及你因此悄悄变化的一点认知。"
            "不用惊天动地——只要不是每天都在发生的就行。"
        )
    return "\n".join(chunks)


__all__ = [
    "COMM_FACT_RULE",
    "build_life_fragment_prompt",
    "maybe_generate_life_fragment",
]
