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
from ..creation.chronicle import (
    build_chronicle_prompt as _build_chronicle_prompt,
    load_native_personality as _load_native_personality,
    maybe_daily_chronicle as _maybe_daily_chronicle,
)
from ..creation.life import (
    build_life_fragment_prompt as _build_life_fragment_prompt,
    maybe_generate_life_fragment as _maybe_generate_life_fragment,
)
from ..creation.seeder import maybe_seed_world_event as _maybe_seed_world_event
from ..deps import Deps
from ..kvkeys import BRANCH_PREFIX as _BRANCH_PREFIX
from ..kvkeys import SELF_SCOPE as _SELF_SCOPE
from ..proactive.sourcing import (
    build_bysource as _build_bysource,
    build_bysource_detail as _build_bysource_detail,
    compute_share_urge as _compute_share_urge,
    record_urge_feedback as _record_urge_feedback,
)
from ..render.audience import filter_entries, visible_events
from ..store import NarrativeStore
from .continuity import append_milestone, current_relationship_stage

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

#: 常驻状态片段 kv 键已收口至 kvkeys（深化 F；_SELF_SCOPE 为 import 别名）

#: state 结构级版本号（R12）。**批 2 前本字段写进 state 却从未被任何代码读取**
#: （F1）——只有 ``updated_ts`` 在写。批 2 起它真正生效：开库时版本不符 → WARN
#: + 原地重置（ADR-0002 §7「旧 state 不迁移，不为死格式陪葬」）。
#: 变更 state 结构（增删字段/改嵌套形状）时必须 +1。
#: 版本史：1 = v0.1.x 初始形状；2 = 批 2（死字段处决 + 关系四维 schema）；
#: 3 = 批 4（自我层 perspective 段：world_view / life_goals + origin/updated_ts 溯源）。
#: OBSERVE(R12)：state **结构级**迁移挂点——开库版本不符即 WARN + 原地重置（chronicle 保留）；
#: 与 store 的表级 `_migrate`（R21）是两回事，别合并别删；改 state 结构时必须 +1。
STATE_SCHEMA_VERSION = 3

# 关系阶段阈值（familiarity，只进不退）已于批 2 删除（ADR-0002 §2）：
# 旧规则线 `stage/familiarity/trust` 由晋升线全量替换，不留双轨。批 2~批 4 空窗期
# 的关系呈现改由 `continuity.current_relationship_stage()` 从**只读事实**推导。

# ─── 生活片段详略分档（2026-09-21 新增，按事件重要度决定创作长度） ──────────
# 档位表（长度/素材展示上限/密度阈/能量阈）已随创作方法迁至
# ``services/creation/life.py``（v0.2.0 批 2-C7）——档位是创作层的概念，
# 常量不该留在只做规则推进的引擎里。
# 入库素材保留上限仍在下方（record_interaction 写事件时用）。
_FRAGMENT_MATERIAL_STORE_CAP = 300

# 消费端注入上限（2026-09-21）：所有把「生活片段 / 编年史 / 由头」送进模型的的地方
# 统一取该值。原为 40~120 字的分散硬编码上限，major 档（可达 400 字）会被截断，
# 且每次调创作长度都要回头改截断值。1024 是防失控的天花板，不是调节旋钮，
# 故不进配置（真正的旋钮已暴露：频率、日上限、分级开关、max_tokens）。
INJECT_TEXT_CAP = 1024

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
        # 自我层「看法」（批 4-C1，ADR-0002 §1）：慢变区的 **general** 维度——
        # 可进通用注入，也是 ``[learned]`` 投影的唯一来源（relationship 是 per_user，
        # 永不进 config.toml）。``origin`` 溯源 seed/promotion/manual，
        # ``updated_ts`` 记最近写入时刻，二者是晋升 diff 的叙述起点。
        "perspective": {
            "world_view": "",
            "life_goals": [],
            "origin": "",
            "updated_ts": "",
        },
        "meta": {"version": STATE_SCHEMA_VERSION, "updated_ts": ""},
    }


def default_branch_state() -> Dict[str, Any]:
    """支线层初始状态（关系锚 identity 由规则登记）。

    关系 schema（批 2 起，ADR-0002 §1）：``relationship`` 四维
    trust/closeness/boundaries/stage + 只读事实 first_met/milestones。
    旧的 ``state.familiarity`` 已删（只进不退的假演化，ADR-0002 §2）。

    ⚠ 批 2~批 4 空窗期：四维**没有任何写入者**（晋升机是批 4）。``stage`` 由
    ``continuity.current_relationship_stage()`` 从只读事实确定性推导呈现。
    """
    return {
        "identity": {
            "first_met": "",
        },
        "relationship": {
            "trust": 0.0,
            "closeness": 0.0,
            "boundaries": 0.0,
            # stage 由前三者 + 证据晋升产生，可回退（批 4）；批 2 由只读事实推导
            "stage": "陌生人",
            # 只读事实（ADR-0002 §2）：是记录不是演化观点，不参与晋升
            "first_met": "",
            "milestones": [],
        },
        "state": {
            "last_interaction_ts": "",
            # OBSERVE(R6)：互动计数器降级为**内部证据计数**——不进注入块、
            # 不进 /narrative status，只作晋升证据链的确定性时间戳来源。
            "interaction_count": 0,
        },
        "meta": {"version": STATE_SCHEMA_VERSION, "updated_ts": ""},
    }


class NarrativeEngine:
    """世界引擎：加载状态 → 规则 tick → 事件入队 → 由头签发。"""

    #: 依赖束（深化 B）。**类级默认 None**：单测常用 ``__new__`` 绕过 ``__init__``，
    #: ``deps`` 属性据此判定走懒组装兼容通道（同 ``_style_inject_count`` 类级默认的理由）。
    _deps: Optional[Deps] = None

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._store: NarrativeStore = plugin._store
        self._creator = CreatorClient(plugin)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._deps = self._assemble_deps()

    # ─── 依赖束（深化 B / grilling Q1a）───────────────────────────────

    @property
    def deps(self) -> Deps:
        """当前依赖快照：真实实例读装配结果，``__new__`` 旧桩现场懒组装。

        懒组装**不缓存**（每次现场取）：旧桩常以可变 ``_plugin`` 驱动用例，
        缓存会让「换 config 再调一次」的既有用例看到陈旧快照。
        """
        if self._deps is not None:
            return self._deps
        return self._assemble_deps()

    def _assemble_deps(self) -> Deps:
        """装配依赖快照（唯一装配实现；构造 / 懒组装 / 重绑定三处共用）。

        可选子系统与旧桩缺字段统一走 getattr——与现行消费点
        （``getattr(engine._plugin, "_telemetry", None)`` 等）宽容语义逐字一致；
        生产实例后建的子系统由 ``on_load`` 尾部 ``rebind_deps()`` 补全。
        """
        plugin = self._plugin
        config = plugin.config
        ctx_config = getattr(plugin.ctx, "config", None)

        def _local_now() -> datetime:
            # 读快照 config 的时区偏移（配置换新经 rebind_deps 重建本闭包）
            return local_now(config.narrative.timezone_offset_hours)

        return Deps(
            config=config,
            store=self._store,
            logger=plugin.ctx.logger,
            local_now=_local_now,
            state=self,
            creator=getattr(self, "_creator", None),
            telemetry=getattr(plugin, "_telemetry", None),
            lorebook=getattr(plugin, "_lorebook", None),
            streams=getattr(plugin, "_streams", None),
            group_streams=getattr(plugin, "_group_streams", None),
            native_config_get=ctx_config.get if ctx_config is not None else None,
        )

    def rebind_deps(self) -> None:
        """按当前 plugin 状态重装依赖快照（``on_load`` 尾部 + 配置热重载调用点）。

        engine 持有的子系统（CreatorClient 等）在 B 各批转换时于此级联刷新；
        plugin 持有的（scheduler / promotion）由 ``on_config_update`` 各自级联。
        """
        self._deps = self._assemble_deps()

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
            state["relationship"]["first_met"] = self._local_now().isoformat(timespec="seconds")
            self._store.set_kv(key, state)
            return state
        if not self._is_current_version(state):
            meta = state.get("meta") if isinstance(state.get("meta"), dict) else {}
            self._warn_legacy_reset(f"支线层({user_id})", meta.get("version"))
            state = default_branch_state()
            state["relationship"]["first_met"] = self._local_now().isoformat(timespec="seconds")
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
        removed = self._store.delete_keys_with_prefix(_BRANCH_PREFIX)
        if self._store.get_kv(_SELF_SCOPE) is not None:
            removed += 1
        # 清完立即重建默认 state（带当前版本号），使下次开库不会误判旧库（F2）
        self._store.set_kv(_SELF_SCOPE, default_self_state())
        return removed

    def reset_for_new_persona(self) -> Dict[str, int]:
        """换人设归零（深化 F / 闸门 A 地基件）：**登记表驱动**的全量人格态清除。

        与 ``reset_state``（R12 换代：只清 self/branch，业务键保留）和
        ``/narrative reset``（用户全清：毯式含 stream_map）的语义分界：

        - 清：``kvkeys.PERSONA_RESET_PREFIXES`` 全部前缀（支线层 / 生活计数 /
          播种 / 由头三表 / 里程碑冷却 / 话题 / 主动计数 / 提案 / 晋升冷却 /
          ``[learned]`` 投影）——旧人格的一切业务痕迹；
        - 清：全部事件队列（支线素材 = 涉私原文，换人设必须清；append-only
          纪律只保护编年史）；
        - 留：``kvkeys.KEEP_PREFIXES``（stream_map / group_stream_map——会话
          基础设施与人格无关，清了主动消息就失联；chronicle:* 幂等标记）与
          编年史正文；
        - ``self`` 键经 ``reset_state`` 重建默认态（带当前 schema_version）。

        Returns:
            ``{"removed": 清除键数, "kept": 保留键数}`` 摘要（status 归零核对用）。
        """
        from ..kvkeys import KEEP_PREFIXES, PERSONA_RESET_PREFIXES

        removed = 0
        for prefix in PERSONA_RESET_PREFIXES:
            removed += self._store.delete_keys_with_prefix(prefix)
        kept = 0
        for prefix in KEEP_PREFIXES:
            kept += len(self._store.get_kv_with_prefix(prefix))
        # 事件队列 = 素材层（涉私原文）；编年史 append-only 刻意保留
        self._store.clear_all_events()
        removed += self.reset_state()
        return {"removed": removed, "kept": kept}

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
        self._dequeue_expired_group_events(current)
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
        # 世界事件播种（批 2 / R40）：四闸自控频率（enabled 默认关），异常不影响规则 tick
        try:
            await self.maybe_seed_world_event(current)
        except Exception as exc:
            self._plugin.ctx.logger.error("世界事件播种异常: %s", exc, exc_info=True)

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
            # branch 层回归（v0.2.1 沉默螺旋修复）：见 _regress_branch_urge 文档串。
            self._regress_branch_urge(float(getattr(pro, "urge_branch_regain", 0.0)))

    def _regress_branch_urge(self, regain: float) -> int:
        """branch 层分享欲系数每 tick 向中性 1.0 回归（沉默螺旋修复，v0.2.1）。

        修复前 ``urge_factor`` **只降不升**：``record_urge_feedback`` 里冷落是唯一
        的即时降项，而 ``user_initiated`` 只抬 self 层，于是约 3 次冷落即触地板
        ``urge_branch_floor``，此后无论用户多主动都回不来——单向棘轮。self 层早已
        靠 tick 回归解决了同一个问题，branch 层此前漏了这一半。

        只处理**已落过** ``urge_factor`` 的支线：缺省即 1.0 中性，既不需要写入，
        也不该给从未被冷落过的用户凭空造出噪声键。

        Returns:
            实际写回的支线数量（贴近中性、无实质变化的不写）。
        """
        if regain <= 0:
            return 0
        written = 0
        for key, branch in self._store.get_kv_with_prefix("branch:").items():
            inner = branch.get("state") if isinstance(branch, dict) else None
            if not isinstance(inner, dict) or "urge_factor" not in inner:
                continue
            try:
                factor = float(inner.get("urge_factor", 1.0))
            except (TypeError, ValueError):
                continue
            new_factor = factor + (1.0 - factor) * regain
            if abs(new_factor - factor) < 0.001:
                continue  # 已贴近中性：不写，避免每 tick 无意义落库
            inner["urge_factor"] = round(max(0.05, min(1.0, new_factor)), 3)
            user_id = str(key).split(":", 1)[1] if ":" in str(key) else str(key)
            self.save_branch_state(user_id, branch)
            written += 1
        return written

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

    def _dequeue_expired_group_events(self, now: datetime) -> None:
        """清理超期的群聊观察事件（R35，默认 60 天窗口）。

        ❗ 为什么必须有：``_dequeue_expired_branch_events`` 只遍历私聊的
        ``mode_user_ids``，群事件**无人清理会无限增长**（事件队列唯一的清道夫
        就是这两个方法）。

        ❗ 清理范围必须是「配置名单 ∪ 已知映射」：只按名单清理的话，用户把某个
        群从 ``observe_group_ids`` 摘掉后，它已积累的历史事件就再也没人扫到，
        变成永久孤儿数据。
        """
        cfg = self._plugin.config.narrative
        days = int(getattr(cfg, "group_event_retention_days", 60) or 0)
        if days <= 0:
            return
        cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds")
        for group_id in self._plugin.observed_group_ids():
            self._store.clear_events_before(f"group:{group_id}", cutoff)

    # ─── 对话素材采集 ────────────────────────────────────────────

    def append_milestone(self, user_id: str, desc: str, now: Optional[datetime] = None) -> bool:
        """薄委托：落一条 engaged 里程碑（实现在 ``state/continuity.py``）。

        保持与 ``build_bysource`` / ``compute_share_urge`` 相同的既有约定：
        实现下沉子模块、engine 留一层委托，对外 API 稳定、测试绑定点不散。
        """
        return append_milestone(self, user_id, desc, now or self._local_now())

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
                # ❗ 这里**不是** ~~R19~~ 的「创作层取材溯源清单」（该项已处决，
                # 且从未落地）。此处是 ADR-0004 第 1 层**写入打标**：对话原文只
                # 对该用户可见（ADR-0004 读取过滤），已实现且在线。
                # store 侧另有 branch:{uid} → uid 的兜底推导，此处显式写是为了溯源可读。
                "source_uid": str(user_id),
                # 2026-09-21：保留长度 80 → 300 字符。入库时不知道未来是否重要，
                # 统一多留原文，由创作层统一按 _FRAGMENT_MATERIAL_CAP=120 展示
                # （2026-09-30：原「按档位」展示已随 tier 删除，改为统一上限）。
                "bysource": normalized[:_FRAGMENT_MATERIAL_STORE_CAP] or "（一条消息）",
            }
        )

    def record_group_material(self, group_id: str, text: str, now: Optional[datetime] = None) -> None:
        """R35：群聊消息落一条**纯观察**事件（不碰任何状态）。

        与私聊 ``record_interaction`` 的关键差别：这里**什么都不更新**——不写支线、
        不更新互动时点、不做“被吵醒”结算、不进晋升证据池（2026-09-29 Q2=B 裁定）。
        只留一份带受众标的原文样本，供未来的三轨路由攒判定规则。

        ❗ ``source_uid = g:<gid>`` 是**硬要求**而非可选项：群素材一旦落空受众标
        就会被当成「通用素材」畅通进私聊（09-21 泄露事故的同型路径）。
        store 侧另有 fail-closed 守卫：拿不到受众标的 ``group:`` 事件会被拒绝写入。

        Args:
            group_id: 群号（**必须非空**；调用方负责 fail-closed 前置校验）。
            text: 群消息原文。
            now: 事件时刻（缺省取当前本地时间）。
        """
        current = now or self._local_now()
        normalized = str(text or "").strip()
        if not normalized:
            return
        self._store.push_event(
            {
                "ts": current.isoformat(timespec="seconds"),
                "scope": f"group:{group_id}",
                # ⚠️ kind 故意**不叫** life/daily：``EVIDENCE_ELIGIBLE_KINDS`` 只认这两类，
                # 群素材因此永不进晋升证据池（隔离三重保险之一）。
                "kind": "group_material",
                "source_uid": f"g:{group_id}",
                "bysource": normalized[:_FRAGMENT_MATERIAL_STORE_CAP] or "（一条消息）",
            }
        )

    def record_branch_feedback(self, user_id: str, now: datetime) -> None:
        """支线层互动落痕（批 2 起降级为**内部证据计数**，R6）。

        旧实现（v0.1.x）在此把 familiarity +0.8 / trust +0.5 并据阈值晋升 stage，
        形成"只进不退的假演化"——ADR-0002 §2 已全量废弃。批 2 起本函数**不再写
        关系四维**（那是批 4 晋升机的职责），只：
        - 更新互动时点（承接窗口、由头时间锚点仍需要它）
        - 累加 ``interaction_count``（R6：晋升证据链的确定性计数来源）

        计数**不进注入块、不进 /narrative status**——它是内部证据，不是呈现值。
        """
        # 与紧邻的 record_interaction 保持一致：任一开关关闭即不再推进。
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            return
        branch = self.load_branch_state(user_id)
        inner = branch["state"]
        inner["last_interaction_ts"] = now.isoformat(timespec="seconds")
        # OBSERVE(R6)：内部证据计数（仅累加，呈现层不得读取）
        inner["interaction_count"] = int(inner.get("interaction_count", 0)) + 1
        self.save_branch_state(user_id, branch)

    # ─── 分享欲 share_urge（v0.1.8 第一步：动机驱动主动时机） ────

    def record_urge_feedback(self, user_id: str, event: str) -> None:
        """分享欲事件反馈：实现已迁至 ``proactive/sourcing.py``（见其文档字符串）。"""
        _record_urge_feedback(self.deps, user_id, event)  # DEPRECATED(B)

    def compute_share_urge(self, user_id: str) -> float:
        """合成当前分享欲：实现已迁至 ``proactive/sourcing.py``（见其文档字符串）。"""
        return _compute_share_urge(self.deps, user_id)  # DEPRECATED(B)

    # ─── 由头签发（主动消息的内容之源） ──────────────────────────

    def build_bysource(self, user_id: str, now: Optional[datetime] = None) -> str:
        """签发主动开口由头：实现已迁至 ``proactive/sourcing.py``（见其文档字符串）。"""
        return _build_bysource(self.deps, user_id, now)  # DEPRECATED(B)

    def build_bysource_detail(
        self, user_id: str, now: Optional[datetime] = None
    ) -> Optional[Dict[str, str]]:
        """签发由头并返回完整署名（批 0 / R41，兑现回执数据源）：实现见 sourcing。"""
        return _build_bysource_detail(self.deps, user_id, now)  # DEPRECATED(B)

    # ─── 每日编年史压缩（唯一常规 LLM 节点） ─────────────────────
    # 实现已迁至 ``creation/chronicle.py``（v0.2.0 批 2-C7「类拆分推迟表」）。
    # 保留薄委托方法：对外 API 不变（33+ 处测试直接绑 engine 实例上的这些名字）。

    async def maybe_daily_chronicle(self, now: Optional[datetime] = None) -> None:
        """当日有互动时，用轻量模型生成一条"今日小结"写入编年史（kind=daily）。

        实现已迁至 ``creation/chronicle.py``（见其文档字符串：日期归属与幂等键）。
        """
        await _maybe_daily_chronicle(self.deps, now)  # DEPRECATED(B)

    async def _load_native_personality(self) -> str:
        """读取主程序原生 [personality].personality（实现已迁 creation/chronicle.py）。"""
        return await _load_native_personality(self.deps)  # DEPRECATED(B)

    # ─── 创作层：生活片段生成器（v0.1.3 新增，唯一的日常 LLM 创作节点） ──
    # 实现已迁至 ``creation/life.py``（v0.2.0 批 2-C7「类拆分推迟表」）。
    # 保留薄委托方法：对外 API 不变，且**入库前守卫**的调用点就在那个模块的出口。

    async def maybe_generate_life_fragment(self, now: Optional[datetime] = None) -> None:
        """创作层消费器：闸门下生成一段"生活片段"（实现已迁 creation/life.py）。"""
        await _maybe_generate_life_fragment(self.deps, now)  # DEPRECATED(B)

    async def maybe_seed_world_event(self, now: Optional[datetime] = None) -> None:
        """世界事件播种 tick（批 2 / R40）：实现见 ``creation/seeder.py``（四闸自控频率）。"""
        await _maybe_seed_world_event(self.deps, now)  # DEPRECATED(B)

    def _build_life_fragment_prompt(
        self,
        now: datetime,
        state: Dict[str, Any],
        materials: Sequence[str],
        persona: str = "",
        highlight: bool = False,
        wake: bool = False,
    ) -> str:
        """构造生活片段生成 prompt（实现已迁 creation/life.py）。"""
        return _build_life_fragment_prompt(  # DEPRECATED(B)
            self.deps, now, state, materials, persona=persona, highlight=highlight, wake=wake
        )

    def _build_chronicle_prompt(
        self,
        now: datetime,
        state: Dict[str, Any],
        materials: Sequence[str],
        persona: str = "",
    ) -> str:
        """构造编年史压缩 prompt（实现已迁 creation/chronicle.py）。"""
        return _build_chronicle_prompt(self.deps, now, state, materials, persona=persona)  # DEPRECATED(B)


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