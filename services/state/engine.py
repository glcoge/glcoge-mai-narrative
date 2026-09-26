"""世界引擎：确定性规则驱动的"生活"推进。

纪律（设计树 R2.3/R2.4 共识）：
- tick 走纯规则，零 LLM —— 作息流转、心情自然衰减、日程到点是代码，不是模型。
- LLM 只在关键节点（每日编年史压缩）登场，且优先走轻量任务（creation_task）。
- LLM 只做"候选里的创作"，禁止自由扩写世界观（防崩坏闸）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..creation.creator import CreatorClient
from ..proactive.sourcing import (
    build_bysource as _build_bysource,
    compute_share_urge as _compute_share_urge,
    record_urge_feedback as _record_urge_feedback,
)
from ..render.audience import filter_entries, visible_events
from ..store import NarrativeStore

# 每日作息阶段（本地 24h 制）
_ROUTINE_PHASES: List[Tuple[int, str]] = [
    (5, "清晨"),
    (9, "上午"),
    (12, "午后"),
    (14, "下午"),
    (18, "晚间"),
    (23, "深夜"),
]

_MOOD_BY_ENERGY: List[Tuple[float, str]] = [
    (0.72, "轻快"),
    (0.40, "平静"),
    (0.20, "低落"),
    (0.00, "疲惫"),
]

#: 常驻状态片段，避免每次初始化重建
_SELF_SCOPE = "self"

#: state 结构级版本号（R12）。**批 2 前本字段写进 state 却从未被任何代码读取**
#: （F1）——只有 ``updated_ts`` 在写。批 2 起它真正生效：开库时版本不符 → WARN
#: + 原地重置（ADR-0002 §7「旧 state 不迁移，不为死格式陪葬」）。
#: 变更 state 结构（增删字段/改嵌套形状）时必须 +1。
#: 版本史：1 = v0.1.x 初始形状；2 = 批 2（死字段处决 + 关系四维 schema）。
STATE_SCHEMA_VERSION = 2

# 关系阶段阈值（familiarity，只进不退）：倒序匹配，取首个 familiarity >= threshold 的标签
_STAGE_THRESHOLDS: List[Tuple[float, str]] = [
    (85, "挚友"),
    (60, "朋友"),
    (30, "熟人"),
]

# ─── 生活片段详略分档（2026-09-21 新增，按事件重要度决定创作长度） ──────────
# 动机：此前所有生活片段一律 40~90 字，重要的事（关系里程碑、状态极端、密集对话）
# 与无事发生时同等篇幅 → 「一笔带过」。改为三档，major 档要求写细。
_FRAGMENT_TIER_LENGTH = {
    "flat": "40~90",    # 分级关闭：旧行为
    "minor": "40~90",   # 无素材：纯状态切片
    "normal": "80~180",  # 有对话素材
    "major": "200~400",  # 命中重要度信号
}
# prompt 里每条素材的展示上限（字符）；入库时统一保留 _FRAGMENT_MATERIAL_STORE_CAP
_FRAGMENT_TIER_MATERIAL_CAP = {"flat": 80, "minor": 80, "normal": 120, "major": 300}
_FRAGMENT_MATERIAL_STORE_CAP = 300
# 素材密度阈值：窗口内对话素材 ≥ 该条数即判 major
_FRAGMENT_MATERIAL_DENSITY = 4

# 消费端注入上限（2026-09-21）：所有把「生活片段 / 编年史 / 由头」送进模型的的地方
# 统一取该值。原为 40~120 字的分散硬编码上限，major 档（可达 400 字）会被截断，
# 且每次调创作长度都要回头改截断值。1024 是防失控的天花板，不是调节旋钮，
# 故不进配置（真正的旋钮已暴露：频率、日上限、分级开关、max_tokens）。
INJECT_TEXT_CAP = 1024
# 状态极端阈值（energy）
_FRAGMENT_ENERGY_LOW = 0.25
_FRAGMENT_ENERGY_HIGH = 0.9

# 由头去复用的 kv 键构造已随 build_bysource 一并移到
# ``services/proactive/sourcing.py``（v0.2.0 批 1「类拆分推迟表」）。


def parse_clock(value: str) -> Optional[time]:
    """解析 HH:MM 字符串为 time；失败返回 None。"""
    try:
        normalized = str(value or "").strip()
        hour_text, _, minute_text = normalized.partition(":")
        return time(hour=int(hour_text), minute=int(minute_text))
    except (ValueError, AttributeError):
        return None


def routine_phase(hour: int) -> str:
    """返回当前作息阶段标签。

    注意：相位表按升序排列，必须**倒序**匹配（取最大的 start_hour ≤ hour），
    否则任意 hour≥5 都会命中首个"清晨"（真机踩坑：阶段永远清晨）。
    """
    for start_hour, label in reversed(_ROUTINE_PHASES):
        if hour >= start_hour:
            return label
    return "深夜"


def mood_by_energy(energy: float) -> str:
    """按精力阈值映射心情标签（确定性）。"""
    for threshold, label in _MOOD_BY_ENERGY:
        if energy >= threshold:
            return label
    return "平静"


def minutes_until_clock(value: str, now: datetime) -> Optional[float]:
    """距**当天**某时刻还有多少分钟；已过或解析失败返回 None。

    只按当天计算，不跨天：睡眠窗口跨午夜时「距入睡」在午夜后会算成很长，
    但那时本来就已睡着（由 ``in_sleep_window`` 拦住），不会误触发临近入睡提示。
    """
    clock = parse_clock(value)
    if clock is None:
        return None
    delta = (datetime.combine(now.date(), clock) - now).total_seconds() / 60
    return delta if delta >= 0 else None


def minutes_since_clock(value: str, now: datetime) -> Optional[float]:
    """距**当天**某时刻已过多少分钟；未到或解析失败返回 None。"""
    clock = parse_clock(value)
    if clock is None:
        return None
    delta = (now - datetime.combine(now.date(), clock)).total_seconds() / 60
    return delta if delta >= 0 else None


def in_sleep_window(now: datetime, sleep_time: str, wake_time: str) -> bool:
    """睡眠窗口判定：支持跨午夜（如 23:30-07:00）。

    任一时刻解析失败（含留空）→ 返回 False，即视作**不睡**。``[narrative].sleep_time``
    留空正是靠这条关闭整个睡眠态，无需额外的 enabled 开关。
    """
    sleep = parse_clock(sleep_time)
    wake = parse_clock(wake_time)
    if sleep is None or wake is None:
        return False
    current = now.time()
    if sleep <= wake:
        return sleep <= current < wake
    return current >= sleep or current < wake


# 日照预期提示（配合 routine_phase：相位标签不带"此刻窗外什么样"的感官信息，
# "下午"既可能是烈日当空也可能是天色将暗）。按小时升序排列，倒序匹配（同款纪律）。
_DAYLIGHT_HINTS: List[Tuple[int, str]] = [
    (6, "天刚亮不久"),
    (9, "上午的阳光正好"),
    (12, "日光还长，太阳挂在半空"),
    (16, "傍晚的光变得很软"),
    (18, "天色暗下来了"),
    (21, "夜已深，窗外全黑了"),
]


def daylight_hint(hour: int) -> str:
    """返回当前小时对应的日照预期描述（确定性规则，零 LLM）。

    给渲染层一个稳定的时间锚点，让状态表达有"日光预期"可依
    （v0.1.4 P1：日照预期时间锚点）。倒序匹配，与 routine_phase 同款纪律。
    """
    for start_hour, hint in reversed(_DAYLIGHT_HINTS):
        if hour >= start_hour:
            return hint
    return "天还没亮"


def local_now(offset_hours: int = 8) -> datetime:
    """按配置时区返回"剧本本地时间"（naive、规整到秒）。

    真机踩坑（2026-08-30）：部分环境（Docker 容器/沙箱）墙钟是 +8 时间，
    但系统时区被注册为 UTC——``datetime.now(timezone.utc)`` 返回的竟是
    墙钟（21:54）而非真 UTC（13:54），再叠加偏移会错 8 小时（相位永远清晨）。

    策略（系统感知）：
    - 系统注册时区 == 剧本时区 → 直接信墙钟 ``datetime.now()``；
    - 系统注册为 UTC（常见 mislabel）→ 视为"墙钟即本地"，也信墙钟；
    - 其余（注册了其他时区且与剧本不同）→ 才用 UTC + 偏移。
    """
    try:
        offset = datetime.now().astimezone()
        system_hours = float(offset.utcoffset().total_seconds() / 3600) if offset.utcoffset() else 0.0
    except (AttributeError, ValueError, TypeError):
        system_hours = 0.0

    wall = datetime.now().replace(microsecond=0)
    if abs(system_hours - float(offset_hours)) < 0.01 or abs(system_hours) < 0.01:
        return wall
    utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return (utc_naive + timedelta(hours=int(offset_hours))).replace(microsecond=0)


def default_self_state() -> Dict[str, Any]:
    """自我层初始状态（锚定层 identity 不在状态内，来自 config）。

    作息**不**存在这里：真源是 ``[narrative].sleep_time / wake_time`` 配置项。
    v0.1 曾把这两个值写死在 state 里（``routine.sleep_time``），但全仓从未读取，
    是死字段，v0.1.10 引入睡眠态时已删除——配置才是唯一真源。
    """
    return {
        "state": {
            "mood": {"label": "平静", "energy": 0.55, "last_shift_ts": ""},
            "routine": {
                "phase": "上午",
                # 睡眠态（v0.1.10）：awake / asleep
                "sleep_state": "awake",
                "asleep_since": "",       # 入睡时刻 ISO；醒来清空
                "last_woken_ts": "",      # 最近一次深夜被吵醒时刻 ISO
                "woken_count": 0,         # 今晚被吵醒次数；醒来时清零
                "sleep_delayed_ts": "",   # 因仍在聊天而推迟入睡的起始时刻 ISO
            },
            "focus": {"pending_events": []},
            "last_interaction_ts": "",
            "last_talk_date": "",
        },
        "meta": {"version": STATE_SCHEMA_VERSION, "updated_ts": ""},
    }


def default_branch_state() -> Dict[str, Any]:
    """支线层初始状态（关系锚 identity 由规则登记）。"""
    return {
        "identity": {
            "first_met": "",
            "stage": "陌生人",
        },
        "state": {
            "trust": 0.0,
            "familiarity": 0.0,
            "last_interaction_ts": "",
            "milestones": [],
        },
        "meta": {"version": STATE_SCHEMA_VERSION, "updated_ts": ""},
    }


class NarrativeEngine:
    """世界引擎：加载状态 → 规则 tick → 事件入队 → 由头签发。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._store: NarrativeStore = plugin._store
        self._creator = CreatorClient(plugin)
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ─── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """启动世界时钟周期任务。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop(), name="narrative-engine-tick")
        interval = max(30, int(self._plugin.config.narrative.clock_tick_minutes) * 60)
        self._plugin.ctx.logger.info(
            "narrative 世界时钟已启动（tick 间隔 %s 分钟）", interval // 60
        )

    async def stop(self) -> None:
        """停止世界时钟周期任务。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reconcile(self) -> None:
        """按当前配置幂等对齐启停状态（供 plugin 看门狗/配置热重载调用）。

        只做 start/stop 判定，不打印无动作日志（避免每 15s 刷屏）。
        """
        cfg = self._plugin.config
        want = bool(cfg.plugin.enabled and cfg.narrative.enabled)
        if want and not self._running:
            self.start()
        elif not want and self._running:
            await self.stop()

    async def _tick_loop(self) -> None:
        """世界时钟循环：按配置间隔执行确定性 tick。"""
        try:
            while self._running:
                try:
                    await self.tick()
                except Exception as exc:
                    self._plugin.ctx.logger.error("世界时钟 tick 异常: %s", exc, exc_info=True)
                interval = max(30, int(self._plugin.config.narrative.clock_tick_minutes) * 60)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    def _local_now(self) -> datetime:
        """按插件配置时区取本地时间（全引擎统一入口）。"""
        return local_now(self._plugin.config.narrative.timezone_offset_hours)

    # ─── 状态访问 ────────────────────────────────────────────────

    def _is_current_version(self, state: Dict[str, Any]) -> bool:
        """判断已存 state 的结构版本是否为当前版本。

        缺失 ``meta`` 或缺 ``version`` 同样视为旧版本（v0.1.x 的写法不统一）。
        """
        meta = state.get("meta")
        if not isinstance(meta, dict):
            return False
        return int(meta.get("version") or 0) == STATE_SCHEMA_VERSION

    def _warn_legacy_reset(self, scope_label: str, found_version: Any) -> None:
        """旧版本 state 被重置时的 WARN（静默重置会让人困惑，必须留痕）。"""
        self._plugin.ctx.logger.warning(
            "narrative %s state 结构版本为 %s（当前 %s）→ 原地重置（ADR-0002 §7："
            "不为死格式陪葬）；编年史不受影响，仍完整保留",
            scope_label,
            found_version,
            STATE_SCHEMA_VERSION,
        )

    def load_self_state(self) -> Dict[str, Any]:
        """读取自我层状态；不存在则初始化，**版本不符则原地重置**。"""
        state = self._store.get_kv(_SELF_SCOPE)
        if state is None:
            state = default_self_state()
            self._store.set_kv(_SELF_SCOPE, state)
            return state
        if not self._is_current_version(state):
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            self._warn_legacy_reset("自我层", meta.get("version"))
            state = default_self_state()
            self._store.set_kv(_SELF_SCOPE, state)
        return state

    def save_self_state(self, state: Dict[str, Any]) -> None:
        """写回自我层状态。"""
        state["meta"]["updated_ts"] = self._local_now().isoformat(timespec="seconds")
        self._store.set_kv(_SELF_SCOPE, state)

    def load_branch_state(self, user_id: str) -> Dict[str, Any]:
        """读取指定用户的支线层状态；不存在则初始化并登记初见，版本不符则重置。"""
        key = f"branch:{user_id}"
        state = self._store.get_kv(key)
        if state is None:
            state = default_branch_state()
            state["identity"]["first_met"] = self._local_now().isoformat(timespec="seconds")
            self._store.set_kv(key, state)
            return state
        if not self._is_current_version(state):
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            self._warn_legacy_reset(f"支线层({user_id})", meta.get("version"))
            state = default_branch_state()
            state["identity"]["first_met"] = self._local_now().isoformat(timespec="seconds")
            self._store.set_kv(key, state)
        return state

    def save_branch_state(self, user_id: str, state: Dict[str, Any]) -> None:
        """写回支线层状态。"""
        state["meta"]["updated_ts"] = self._local_now().isoformat(timespec="seconds")
        self._store.set_kv(f"branch:{user_id}", state)

    def reset_state(self) -> int:
        """重置三层运行态：清空 kv 中的 self/branch 状态，**保留编年史与 schema 版本**。

        与 ``/narrative reset`` 的区别：命令侧是全清 kv（含计数键），本方法只清
        **状态类**键，用于「state 换代」语义（R12）。返回清除的键数。
        """
        removed = self._store.delete_keys_with_prefix("branch:")
        if self._store.get_kv(_SELF_SCOPE) is not None:
            removed += 1
        # 清完立即重建默认 state（带当前版本号），使下次开库不会误判旧库（F2）
        self._store.set_kv(_SELF_SCOPE, default_self_state())
        return removed

    # ─── 规则 tick ───────────────────────────────────────────────

    async def tick(self, now: Optional[datetime] = None) -> None:
        """确定性生活推进：精力衰减、心情映射、作息流转、事件出队。"""
        current = now or self._local_now()
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            # 正常情况下 reconcile 会直接停掉 tick 循环，这里是防御分支；
            # 每 tick 打 INFO 会刷屏（v0.1.4 降 debug）
            self._plugin.ctx.logger.debug("narrative tick@%s 跳过（剧本开关未开）", current.strftime("%H:%M"))
            return

        state = self.load_self_state()
        self._apply_state_rules(state, current)
        self.save_self_state(state)
        inner = state["state"]
        # 心跳日志降 debug（每 30min 一条，部署后无排查价值，v0.1.4）
        self._plugin.ctx.logger.debug(
            "narrative tick@%s: phase=%s mood=%s energy=%.2f",
            current.strftime("%Y-%m-%d %H:%M"),
            inner["routine"]["phase"],
            inner["mood"]["label"],
            float(inner["mood"].get("energy", 0)),
        )
        self._snapshot_if_day_changed(state, current)
        self._dequeue_expired_branch_events(current)
        # 创作层：按间隔+上限闸门尝试生成生活片段（内部自控频率，失败不影响规则 tick）
        try:
            await self.maybe_generate_life_fragment(current)
        except Exception as exc:
            self._plugin.ctx.logger.error("生活片段生成异常: %s", exc, exc_info=True)
        # 每日编年史压缩（2026-09-16 接线；此前是死代码，从未被调用）
        # 同样包 try/except：创作层异常不得影响规则 tick
        try:
            await self.maybe_daily_chronicle(current)
        except Exception as exc:
            self._plugin.ctx.logger.error("编年史压缩异常: %s", exc, exc_info=True)

    def _apply_state_rules(self, state: Dict[str, Any], now: datetime) -> None:
        """纯规则：作息与睡眠流转、精力衰减/回升、心情映射、日程到点。"""
        inner = state["state"]
        routine = inner.setdefault("routine", {})
        energy = float(inner["mood"].get("energy", 0.55))
        old_label = str(inner["mood"].get("label", "平静"))

        # 睡眠流转（v0.1.10）：必须在精力规则**之前**跑——睡眠恢复量要读本次判定后的状态
        self._update_sleep_state(state, now)
        asleep = str(routine.get("sleep_state", "awake")) == "asleep"
        woken = self._woken_active(state, now)

        # 精力（v0.1.5 重构，三参数入 config [narrative]）：
        # ① 向基线回归（双向：低于基线回升、高于基线回落）——老版注释声称"向基线
        #    衰减"但无回归项，每 tick 无条件 -0.06，无互动日必然贴地 0.05 卡死；
        # ② 近期互动（2h 内）提振；
        # ③ 深夜睡眠恢复——老版深夜反而 -0.08（睡觉掉精力），方向反了。
        cfg = self._plugin.config.narrative
        energy += (float(cfg.energy_baseline) - energy) * float(cfg.energy_baseline_pull)
        last_interaction = inner.get("last_interaction_ts", "")
        if last_interaction and self._hours_since(last_interaction, now) <= 2:
            energy += float(cfg.energy_interaction_boost)
        # 睡眠恢复（v0.1.10）：判定由「深夜相位」改为「真的睡着且没被吵醒」。
        # 旧写法只看相位：05:00 到起床之间睡着却不回血（漏），23:00-23:30 明明醒着
        # 反而回血（错）。被吵醒时同样不回血——醒了就是醒了。
        if asleep and not woken:
            energy += float(cfg.energy_sleep_recovery)
        energy = max(0.05, min(1.0, energy))

        new_label = mood_by_energy(energy)
        if new_label != old_label:
            inner["mood"] = {
                "label": new_label,
                "energy": round(energy, 3),
                "last_shift_ts": now.isoformat(timespec="seconds"),
            }
        else:
            inner["mood"]["energy"] = round(energy, 3)

        inner["routine"]["phase"] = routine_phase(now.hour)

        # share_urge self 层回归（v0.1.8 第一步）：每 tick 向基线双向回归
        # （同 energy 基线回归模式）。事件驱动的升降在 record_urge_feedback，
        # 此处只管"时间回归"——被冷落的低谷随时间自然回温。
        pro = self._plugin.config.proactive
        if pro.urge_enabled:
            urge_base = float(pro.urge_base)
            urge = float(inner.get("urge", urge_base))
            urge += (urge_base - urge) * float(pro.urge_regain)
            inner["urge"] = round(max(0.05, min(1.0, urge)), 3)

    @staticmethod
    def _hours_since(iso_ts: str, now: datetime) -> float:
        """计算 ISO 时间戳距今的小时数。"""
        try:
            ts = datetime.fromisoformat(iso_ts)
            return (now - ts).total_seconds() / 3600
        except (TypeError, ValueError):
            return float("inf")

    @staticmethod
    def _minutes_since(iso_ts: str, now: datetime) -> Optional[float]:
        """计算 ISO 时间戳距今的分钟数；空/损坏时间戳返回 None（由调用方判空）。"""
        if not iso_ts:
            return None
        try:
            return (now - datetime.fromisoformat(iso_ts)).total_seconds() / 60
        except (TypeError, ValueError):
            return None

    # ─── 睡眠态（v0.1.10）───────────────────────────────────────────

    def _sleep_configured(self) -> bool:
        """睡眠态是否已配置（sleep_time 与 wake_time 均可解析）。留空即关闭整个睡眠态。"""
        cfg = self._plugin.config.narrative
        return parse_clock(cfg.sleep_time) is not None and parse_clock(cfg.wake_time) is not None

    def _sleep_window_active(self, now: datetime) -> bool:
        """当前时刻是否落在配置的睡眠窗口内（跨午夜安全；未配置恒 False）。"""
        cfg = self._plugin.config.narrative
        return in_sleep_window(now, cfg.sleep_time, cfg.wake_time)

    def is_asleep(self, now: Optional[datetime] = None) -> bool:
        """客观是否睡着。不看「被吵醒」——那是瞬时状态，不改变 sleep_state。"""
        current = now or self._local_now()
        state = self.load_self_state()
        routine = state["state"].setdefault("routine", {})
        if str(routine.get("sleep_state", "awake")) != "asleep":
            return False
        return self._sleep_window_active(current)

    def _woken_active(self, state: Dict[str, Any], now: datetime) -> bool:
        """「被吵醒」的瞬时状态是否仍生效：睡着 + 吵醒后 woken_awake_minutes 内。"""
        routine = state["state"].get("routine", {})
        if str(routine.get("sleep_state", "awake")) != "asleep":
            return False
        minutes = int(self._plugin.config.narrative.woken_awake_minutes)
        if minutes <= 0:
            return False
        elapsed = self._minutes_since(str(routine.get("last_woken_ts", "") or ""), now)
        return elapsed is not None and elapsed < minutes

    def _is_still_talking(self, state: Dict[str, Any], now: datetime) -> bool:
        """最近一次互动是否落在「仍在聊」窗口内（决定要不要推迟入睡）。"""
        recent = int(self._plugin.config.narrative.sleep_delay_recent_minutes)
        if recent <= 0:
            return False
        elapsed = self._minutes_since(str(state["state"].get("last_interaction_ts", "") or ""), now)
        return elapsed is not None and elapsed <= recent

    def _delay_budget_left(self, delayed_since: str, now: datetime) -> bool:
        """推迟入睡是否还有余量。首次推迟（无起始时刻）必有余量。"""
        max_minutes = int(self._plugin.config.narrative.sleep_delay_max_minutes)
        if max_minutes <= 0:
            return False
        if not delayed_since:
            return True
        elapsed = self._minutes_since(delayed_since, now)
        return elapsed is None or elapsed < max_minutes

    def _update_sleep_state(self, state: Dict[str, Any], now: datetime) -> None:
        """睡眠状态机（纯规则，零 LLM）。

        三条规则：
        ① 时间兜底：落在睡眠窗口内 → 入睡；落在窗口外 → 醒来（并挂「起床补一段」标）。
        ② 事件修饰：到 sleep_time 时若仍在聊天 → 推迟入睡，最多 sleep_delay_max_minutes，
           超过上限强制入睡（否则「聊到天亮」就永远不睡了）。
        ③ 瞬时例外（深夜被吵醒）**不改**状态位——那是 ``_apply_woken_penalty`` 的事，
           被吵醒只记时刻、计数、扣精力，bot 仍然算睡着。
        """
        cfg = self._plugin.config.narrative
        routine = state["state"].setdefault("routine", {})
        current_state = str(routine.get("sleep_state", "awake"))

        if not self._sleep_window_active(now):
            if current_state == "asleep":
                routine["sleep_state"] = "awake"
                routine["asleep_since"] = ""
                routine["last_woken_ts"] = ""
                routine["woken_count"] = 0
                # 起床补一段：由创作层消费（豁免间隔闸门与日上限）
                routine["wake_fragment_pending"] = True
                self._plugin.ctx.logger.info("narrative 睡眠: 醒来（%s）", now.strftime("%H:%M"))
            routine["sleep_delayed_ts"] = ""
            return

        if current_state == "awake":
            delayed_since = str(routine.get("sleep_delayed_ts", "") or "")
            if self._is_still_talking(state, now) and self._delay_budget_left(delayed_since, now):
                if not delayed_since:
                    routine["sleep_delayed_ts"] = now.isoformat(timespec="seconds")
                    self._plugin.ctx.logger.info(
                        "narrative 睡眠: 到点但仍在聊，推迟入睡（上限 %s 分钟）",
                        int(cfg.sleep_delay_max_minutes),
                    )
                return
            routine["sleep_state"] = "asleep"
            routine["asleep_since"] = now.isoformat(timespec="seconds")
            routine["sleep_delayed_ts"] = ""
            self._plugin.ctx.logger.info("narrative 睡眠: 入睡（%s）", now.strftime("%H:%M"))

    def _apply_woken_penalty(self, state: Dict[str, Any], now: datetime) -> None:
        """睡眠中收到消息 → 记「被吵醒」时刻 + 计数 + 扣精力（一夜多次有地板）。

        瞬时语义：**不**改 sleep_state。bot 仍然算睡着，只是这一轮有点迷糊、
        精力掉一截，``woken_awake_minutes`` 后自动回落。
        """
        cfg = self._plugin.config.narrative
        routine = state["state"].setdefault("routine", {})
        if str(routine.get("sleep_state", "awake")) != "asleep":
            return
        routine["last_woken_ts"] = now.isoformat(timespec="seconds")
        routine["woken_count"] = int(routine.get("woken_count", 0) or 0) + 1
        penalty = float(cfg.energy_woken_penalty)
        if penalty <= 0:
            return
        floor = float(cfg.energy_woken_floor)
        mood = state["state"].setdefault("mood", {})
        before = float(mood.get("energy", 0.55))
        mood["energy"] = round(max(floor, before - penalty), 3)
        mood["last_shift_ts"] = now.isoformat(timespec="seconds")
        self._plugin.ctx.logger.info(
            "narrative 睡眠: 深夜被吵醒（今晚第 %s 次，精力 %.2f → %.2f）",
            routine["woken_count"], before, mood["energy"],
        )

    def _snapshot_if_day_changed(self, state: Dict[str, Any], now: datetime) -> None:
        """跨日时保存一份状态快照（回滚点 + 验收指标 4 原料）。"""
        today = now.strftime("%Y-%m-%d")
        snapshots = self._store.list_snapshots()
        if snapshots and snapshots[0] == today:
            return
        self._store.save_snapshot(today, state)

    def _dequeue_expired_branch_events(self, now: datetime) -> None:
        """清理 3 天前的支线事件（事件队列有界）。"""
        cutoff = (now - timedelta(days=3)).isoformat(timespec="seconds")
        for user_id in self._plugin.config.narrative.mode_user_ids or []:
            self._store.clear_events_before(f"branch:{user_id}", cutoff)

    # ─── 对话素材采集 ────────────────────────────────────────────

    def record_interaction(self, user_id: str, text: str, now: Optional[datetime] = None) -> None:
        """用户互动落痕：更新自我层互动时点，素材入支线事件队列。"""
        current = now or self._local_now()
        today = current.strftime("%Y-%m-%d")
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            return

        state = self.load_self_state()
        state["state"]["last_interaction_ts"] = current.isoformat(timespec="seconds")
        state["state"]["last_talk_date"] = today
        # 深夜被吵醒（v0.1.10）：睡着时收到消息 → 记时刻 + 计数 + 扣精力
        self._apply_woken_penalty(state, current)
        self.save_self_state(state)

        normalized = str(text or "").strip()
        if not normalized:
            return
        # 素材入支线事件队列供创作层消费（批 2 已删废弃的 hot_thread：该字段在
        # v0.1.3 起就无写入点，"心头事"改由创作层/生活片段独占）
        self._store.push_event(
            {
                "ts": current.isoformat(timespec="seconds"),
                "scope": f"branch:{user_id}",
                "kind": "dialogue_material",
                # RESERVED(R19)：素材溯源。对话原文只对该用户可见（ADR-0004 读取过滤），
                # store 侧另有 branch:{uid} → uid 的兜底推导，此处显式写是为了溯源可读。
                "source_uid": str(user_id),
                # 2026-09-21：保留长度 80 → 300 字符。入库时不知道未来是否重要，
                # 统一多留原文，由创作层按档位决定展示多少（见 _FRAGMENT_TIER_MATERIAL_CAP）
                "bysource": normalized[:_FRAGMENT_MATERIAL_STORE_CAP] or "（一条消息）",
            }
        )

    def record_branch_feedback(self, user_id: str, now: datetime) -> None:
        """支线层反馈：信任/熟悉度小步增长；里程碑只进不退。"""
        # 与紧邻的 record_interaction 保持一致：任一开关关闭即不再推进关系值。
        # （此前本函数无 gate，剧本关闭期间关系值/里程碑仍在涨）
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            return
        branch = self.load_branch_state(user_id)
        inner = branch["state"]
        inner["last_interaction_ts"] = now.isoformat(timespec="seconds")
        inner["familiarity"] = round(min(100.0, float(inner.get("familiarity", 0.0)) + 0.8), 1)
        inner["trust"] = round(min(100.0, float(inner.get("trust", 0.0)) + 0.5), 1)

        stage = str(branch["identity"].get("stage", "陌生人"))
        familiarity = float(inner["familiarity"])
        milestones = list(inner.get("milestones", []))
        for threshold, label in _STAGE_THRESHOLDS:
            if familiarity >= threshold:
                # 阈值从高到低，命中首个即 familiarity 能达到的最高档；未到该档才晋升（只进不退）
                if stage != label:
                    old_stage = stage
                    branch["identity"]["stage"] = label
                    if not any(item.get("id") == f"stage:{label}" for item in milestones):
                        milestones.append(
                            {
                                "id": f"stage:{label}",
                                "ts": now.isoformat(timespec="seconds"),
                                "desc": f"你们从{old_stage}变成了{label}",
                                "stage": "done",
                            }
                        )
                    inner["milestones"] = milestones
                break
        self.save_branch_state(user_id, branch)

    # ─── 分享欲 share_urge（v0.1.8 第一步：动机驱动主动时机） ────

    def record_urge_feedback(self, user_id: str, event: str) -> None:
        """分享欲事件反馈：实现已迁至 ``proactive/sourcing.py``（见其文档字符串）。"""
        _record_urge_feedback(self, user_id, event)

    def compute_share_urge(self, user_id: str) -> float:
        """合成当前分享欲：实现已迁至 ``proactive/sourcing.py``（见其文档字符串）。"""
        return _compute_share_urge(self, user_id)

    # ─── 由头签发（主动消息的内容之源） ──────────────────────────

    def build_bysource(self, user_id: str, now: Optional[datetime] = None) -> str:
        """签发主动开口由头：实现已迁至 ``proactive/sourcing.py``（见其文档字符串）。"""
        return _build_bysource(self, user_id, now)

    # ─── 每日编年史压缩（唯一常规 LLM 节点） ─────────────────────

    async def maybe_daily_chronicle(self, now: Optional[datetime] = None) -> None:
        """当日有互动时，用轻量模型生成一条"今日小结"写入编年史（kind=daily）。

        日期归属（2026-09-16 接线时修正）：目标日期 = **触发点所属的那一天**。
        旧写法是 ``current.time() < trigger → return``，但 tick 间隔 30 分钟，
        若错过 ``[trigger, 24:00)`` 窗口（典型：23:30 触发、tick 落在 00:00），
        判定就会一直不成立 → **功能永不执行**。故过了午夜要能补写昨天。
        """
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            return
        if not cfg.narrative.chronicle_enabled:
            return
        trigger = parse_clock(cfg.narrative.daily_chronicle_time)
        if trigger is None:
            return

        current = now or self._local_now()
        state = self.load_self_state()

        # 睡眠态启用时，等**真正入睡**才写：一天到入睡才算结束。
        # 旧行为是「过了 daily_chronicle_time 就写」，但入睡可能因仍在聊被推迟
        # （最多 sleep_delay_max_minutes），那样小结会漏掉入睡前的最后一段对话。
        if self._sleep_configured() and str(
            state["state"].get("routine", {}).get("sleep_state", "awake")
        ) != "asleep":
            return

        target_date = current.date()
        if current.time() < trigger:
            target_date = target_date - timedelta(days=1)
        date_text = target_date.strftime("%Y-%m-%d")

        # 幂等统一走 store 的标准键。旧代码手写的 ``chronicle:done:{today}``
        # 与 is_chronicle_done 用的 ``chronicle:{scope}:{kind}:{date}`` 是两套并存。
        if self._store.is_chronicle_done(_SELF_SCOPE, "daily", date_text):
            return

        if state["state"].get("last_talk_date") != date_text:
            return  # 那天没说过话，不写

        materials: List[str] = []
        for user_id in (cfg.narrative.mode_user_ids or []):
            # 受众过滤（ADR-0004）：此处 scope 已是 branch:{user_id}，当前等价于原行为；
            # 走过滤入口是为跨用户取材（C4）预留——届时涉私原文不会进创作 prompt。
            for item in visible_events(self._store, f"branch:{user_id}", user_id, 50):
                if str(item.get("ts", "")).startswith(date_text):
                    materials.append(str(item.get("bysource", "")))

        persona = await self._load_native_personality()
        # prompt 里的日期取自入参 now，故传目标日期而非当前时刻
        target_dt = datetime.combine(target_date, trigger)
        prompt = self._build_chronicle_prompt(target_dt, state, materials, persona=persona)
        if cfg.llm.show_prompt:
            self._plugin.ctx.logger.info("编年史 prompt: %s", prompt[:300])

        text = await self._creator.generate(prompt)
        if text:
            self._store.append_chronicle(
                _SELF_SCOPE, "daily", text, target_dt.isoformat(timespec="seconds")
            )
            self._plugin.ctx.logger.info("编年史今日小结已写入: %s", date_text)
        self._store.mark_chronicle_done(_SELF_SCOPE, "daily", date_text)

    async def _load_native_personality(self) -> str:
        """读取主程序原生 [personality].personality（人设唯一来源，失败降级为空串）。"""
        try:
            return str(await self._plugin.ctx.config.get("personality.personality", "") or "").strip()
        except Exception as exc:
            self._plugin.ctx.logger.debug("读取原生 personality 失败: %s", exc)
            return ""

    # ─── 创作层：生活片段生成器（v0.1.3 新增，唯一的日常 LLM 创作节点） ──

    async def maybe_generate_life_fragment(self, now: Optional[datetime] = None) -> None:
        """创作层消费器：间隔 + 每日上限闸门下，用轻量模型生成一段"生活片段"。

        目的：让 bot 的"生活"不只是数字变化，而是有一段段可被对话引用的生活故事
        （修复主动消息复述问题的第一环）。生成结果双写：
        - ``focus.pending_events``（有界 5 条，渲染时引用为"最近的生活片段"）；
        - ``chronicle``（append-only，kind=life，日记侧可见）。

        只读事件队列，不消费（三个原因：编年史 23:30 也要读同一批素材；
        事件有每日上限本来就有界；3 天前的由 _dequeue_expired_branch_events 清理）。
        """
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            return
        if int(cfg.narrative.life_fragment_daily_max) <= 0:
            return

        current = now or self._local_now()
        today = current.strftime("%Y-%m-%d")
        state = self.load_self_state()
        routine = state["state"].setdefault("routine", {})
        day_count = self._store.get_kv_int(f"life_fragment:count:{today}")

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
            last_ts = self._store.get_kv_str("life_fragment:last_ts")
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
            for item in visible_events(self._store, f"branch:{user_id}", user_id, 20):
                source_text = str(item.get("bysource", "") or "").strip()
                if source_text:
                    materials.append(source_text)

        # 详略档位：按事件重要度决定本次创作长度（flat=分级关闭，回旧行为）
        tier = self._life_fragment_tier(current, state, window_start)
        persona = await self._load_native_personality()
        prompt = self._build_life_fragment_prompt(
            current, state, materials, persona=persona, tier=tier, wake=wake_fragment
        )
        if cfg.llm.show_prompt:
            self._plugin.ctx.logger.info("生活片段 prompt: %s", prompt[:300])

        text = await self._creator.generate(prompt)
        if not text:
            return

        # 指标 4 双轨埋点（R24 A 轨 / R7 B 轨）：只在产出这一刻采样，
        # 保证"注入侧组合熵"与"产出侧文本多样性"同尺度可比。
        telemetry = getattr(self._plugin, "_telemetry", None)
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
        self.save_self_state(state)
        # 生活片段照常生成（pending_events 是主动消息的由头来源，不能断），
        # 仅"写入编年史"这一步受 chronicle_enabled 约束（2026-09-16：
        # 此前该开关管不到 life，名不副实）
        if cfg.narrative.chronicle_enabled:
            self._store.append_chronicle(
                _SELF_SCOPE, "life", text, current.isoformat(timespec="seconds")
            )

        # 闸门推进 + 计数（last_ts 直接存 ISO 字符串，不再用 dict 包装）
        # 起床补一段豁免日上限（它是状态转换的必然产物，不是可选的创作），
        # 但仍推进 last_ts——否则醒来后第一段正常片段会紧接着挤进来。
        self._store.set_kv_str("life_fragment:last_ts", current.isoformat(timespec="seconds"))
        if not wake_fragment:
            self._store.set_kv_int(f"life_fragment:count:{today}", day_count + 1)
        self._plugin.ctx.logger.info(
            "生活片段已生成（今日 %s/%s，档位 %s%s）: %s",
            day_count if wake_fragment else day_count + 1,
            cfg.narrative.life_fragment_daily_max,
            tier,
            "，起床补一段" if wake_fragment else "",
            text[:40],
        )

    def _life_fragment_tier(
        self, now: datetime, state: Dict[str, Any], window_start: datetime
    ) -> str:
        """判定本次生活片段的详略档位（flat/minor/normal/major）。

        2026-09-21 定案的三信号组合（纯规则，零额外 LLM 调用）：
        ① 关系里程碑：窗口内支线 ``milestones`` 有新增（关系阶段晋升，强信号）；
        ② 状态极端：self 层 energy ≤ 低阈 或 ≥ 高阈；
        ③ 素材密度：窗口内对话素材条数 ≥ 阈值。

        任一命中 → major；无命中但有素材 → normal；无素材 → minor。
        ``[narrative].life_fragment_detail_enabled=false`` 时返回 flat（旧行为）。
        """
        cfg = self._plugin.config
        if not cfg.narrative.life_fragment_detail_enabled:
            return "flat"

        energy = float(state["state"]["mood"].get("energy", 0.0))
        if energy <= _FRAGMENT_ENERGY_LOW or energy >= _FRAGMENT_ENERGY_HIGH:
            return "major"

        start_iso = window_start.isoformat(timespec="seconds")
        material_count = 0
        for user_id in (cfg.narrative.mode_user_ids or []):
            branch = self.load_branch_state(user_id)
            for item in branch["state"].get("milestones") or []:
                if str(item.get("ts", "")) >= start_iso:
                    return "major"
            for event in visible_events(self._store, f"branch:{user_id}", user_id, 20):
                if str(event.get("ts", "")) >= start_iso and str(event.get("bysource", "")).strip():
                    material_count += 1

        if material_count >= _FRAGMENT_MATERIAL_DENSITY:
            return "major"
        return "normal" if material_count else "minor"

    def _build_life_fragment_prompt(
        self,
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
        cfg = self._plugin.config
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

    def _build_chronicle_prompt(
        self,
        now: datetime,
        state: Dict[str, Any],
        materials: Sequence[str],
        persona: str = "",
    ) -> str:
        """构造编年史压缩 prompt（40~90 字的一日小结，第一人称）。"""
        cfg = self._plugin.config
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
    "NarrativeEngine",
    "local_now",
    "parse_clock",
    "routine_phase",
    "daylight_hint",
    "mood_by_energy",
    "default_self_state",
    "default_branch_state",
]