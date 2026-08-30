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

from .store import NarrativeStore

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

# 常驻状态片段，避免每次初始化重建
_SELF_SCOPE = "self"


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
    """自我层初始状态（锚定层 identity 不在状态内，来自 config）。"""
    return {
        "state": {
            "mood": {"label": "平静", "energy": 0.55, "last_shift_ts": ""},
            "routine": {"phase": "上午", "sleep_time": "23:30", "wake_time": "07:00"},
            "focus": {"hot_thread": "", "pending_events": []},
            "habits": [],
            "last_interaction_ts": "",
            "last_talk_date": "",
        },
        "meta": {"version": 1, "updated_ts": ""},
    }


def default_branch_state() -> Dict[str, Any]:
    """支线层初始状态（关系锚 identity 由规则登记）。"""
    return {
        "identity": {
            "first_met": "",
            "stage": "陌生人",
            "shared_secrets": [],
        },
        "state": {
            "trust": 0.0,
            "familiarity": 0.0,
            "last_interaction_ts": "",
            "milestones": [],
            "user_notes": {},
        },
        "meta": {"version": 1, "updated_ts": ""},
    }


class NarrativeEngine:
    """世界引擎：加载状态 → 规则 tick → 事件入队 → 由头签发。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._store: NarrativeStore = plugin._store
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

    def load_self_state(self) -> Dict[str, Any]:
        """读取自我层状态；不存在则初始化。"""
        state = self._store.get_kv(_SELF_SCOPE)
        if state is None:
            state = default_self_state()
            self._store.set_kv(_SELF_SCOPE, state)
        return state

    def save_self_state(self, state: Dict[str, Any]) -> None:
        """写回自我层状态。"""
        state["meta"]["updated_ts"] = self._local_now().isoformat(timespec="seconds")
        self._store.set_kv(_SELF_SCOPE, state)

    def load_branch_state(self, user_id: str) -> Dict[str, Any]:
        """读取指定用户的支线层状态；不存在则初始化并登记初见。"""
        key = f"branch:{user_id}"
        state = self._store.get_kv(key)
        if state is None:
            state = default_branch_state()
            state["identity"]["first_met"] = self._local_now().isoformat(timespec="seconds")
            self._store.set_kv(key, state)
        return state

    def save_branch_state(self, user_id: str, state: Dict[str, Any]) -> None:
        """写回支线层状态。"""
        state["meta"]["updated_ts"] = self._local_now().isoformat(timespec="seconds")
        self._store.set_kv(f"branch:{user_id}", state)

    # ─── 规则 tick ───────────────────────────────────────────────

    async def tick(self, now: Optional[datetime] = None) -> None:
        """确定性生活推进：精力衰减、心情映射、作息流转、事件出队。"""
        current = now or self._local_now()
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            self._plugin.ctx.logger.info("narrative tick@%s 跳过（剧本开关未开）", current.strftime("%H:%M"))
            return

        state = self.load_self_state()
        self._apply_state_rules(state, current)
        self.save_self_state(state)
        inner = state["state"]
        self._plugin.ctx.logger.info(
            "narrative tick@%s: phase=%s mood=%s energy=%.2f",
            current.strftime("%Y-%m-%d %H:%M"),
            inner["routine"]["phase"],
            inner["mood"]["label"],
            float(inner["mood"].get("energy", 0)),
        )
        self._snapshot_if_day_changed(state, current)
        self._dequeue_expired_branch_events(current)

    def _apply_state_rules(self, state: Dict[str, Any], now: datetime) -> None:
        """纯规则：精力衰减/回升、心情映射、作息阶段、日程到点。"""
        inner = state["state"]
        energy = float(inner["mood"].get("energy", 0.55))
        old_label = str(inner["mood"].get("label", "平静"))

        # 精力：向基线 0.45 衰减；近期互动（2h 内）回升一档
        energy -= 0.06
        last_interaction = inner.get("last_interaction_ts", "")
        if last_interaction and self._hours_since(last_interaction, now) <= 2:
            energy += 0.12
        if routine_phase(now.hour) == "深夜":
            energy -= 0.08
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

    @staticmethod
    def _hours_since(iso_ts: str, now: datetime) -> float:
        """计算 ISO 时间戳距今的小时数。"""
        try:
            ts = datetime.fromisoformat(iso_ts)
            return (now - ts).total_seconds() / 3600
        except (TypeError, ValueError):
            return float("inf")

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
        self.save_self_state(state)

        normalized = str(text or "").strip()
        if not normalized:
            return
        # 由头：把"用户说了点什么"变成"bot 心里记挂的焦点"（确定性规则）
        if len(normalized) >= 12:
            state = self.load_self_state()
            state["state"]["focus"]["hot_thread"] = normalized[:60]
            self.save_self_state(state)

        self._store.push_event(
            {
                "ts": current.isoformat(timespec="seconds"),
                "scope": f"branch:{user_id}",
                "kind": "dialogue_material",
                "bysource": normalized[:80] or "（一条消息）",
            }
        )

    # ─── 由头签发（主动消息的内容之源） ──────────────────────────

    def build_bysource(self, user_id: str, now: Optional[datetime] = None) -> str:
        """从状态机签发"主动开口的由头"：由头永远来自生活，禁止干聊。"""
        current = now or self._local_now()
        cfg = self._plugin.config
        state = self.load_self_state()
        branch = self.load_branch_state(user_id)

        candidates: List[str] = []
        hot_thread = str(state["state"]["focus"].get("hot_thread", "")).strip()
        if hot_thread:
            candidates.append(f"今天心里一直挂着这件事：{hot_thread}")

        mood = str(state["state"]["mood"].get("label", "平静"))
        if mood in ("低落", "疲惫"):
            candidates.append(f"今天有点{mood}，想找人聊聊")

        phase = str(state["state"]["routine"].get("phase", ""))
        if phase == "深夜":
            candidates.append("夜深了，我还不想睡，想跟你说点什么")
        elif phase == "清晨":
            candidates.append("刚醒，今天莫名的想先跟你说句话")

        stage = str(branch["identity"].get("stage", "陌生人"))
        milestones = list(branch["state"].get("milestones", []))
        if milestones and stage != "陌生人":
            latest_milestone = milestones[-1]
            candidates.append(f"想起我们之间那件事：{latest_milestone.get('desc', '')}")

        if not candidates:
            world = str(cfg.identity.world or "这个城市")
            candidates.append(f"在{world}里度过了平静的一天，突然想跟你打声招呼")

        # 场景感补全：把由头变成"生活片段"式的开口（确定性选一个，按小时稳定）
        seed = sum(ord(char) for char in user_id) + current.hour
        return candidates[seed % len(candidates)]

    # ─── 每日编年史压缩（唯一常规 LLM 节点） ─────────────────────

    async def maybe_daily_chronicle(self, now: Optional[datetime] = None) -> None:
        """当日有互动时，用轻量模型生成一条"今日小结"写入编年史。"""
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled:
            return
        if not cfg.narrative.chronicle_enabled:
            return
        trigger = parse_clock(cfg.narrative.daily_chronicle_time)
        if trigger is None:
            return

        current = now or self._local_now()
        if current.time() < trigger:
            return
        today = current.strftime("%Y-%m-%d")
        if self._store.get_kv_int(f"chronicle:done:{today}") > 0:
            return

        state = self.load_self_state()
        if state["state"].get("last_talk_date") != today:
            return  # 今天没说过话，不写

        materials: List[str] = []
        for user_id in (cfg.narrative.mode_user_ids or []):
            for item in self._store.list_events(f"branch:{user_id}", limit=50):
                if str(item.get("ts", "")).startswith(today):
                    materials.append(str(item.get("bysource", "")))

        persona = await self._load_native_personality()

        prompt = self._build_chronicle_prompt(current, state, materials, persona=persona)
        if cfg.llm.show_prompt:
            self._plugin.ctx.logger.info("编年史 prompt: %s", prompt[:300])

        text = await self._call_creation_llm(prompt)
        if text:
            self._store.append_chronicle(_SELF_SCOPE, "daily", text, current.isoformat(timespec="seconds"))
            self._plugin.ctx.logger.info("编年史今日小结已写入: %s", today)
        self._store.set_kv_int(f"chronicle:done:{today}", 1)

    async def _load_native_personality(self) -> str:
        """读取主程序原生 [personality].personality（人设唯一来源，失败降级为空串）。"""
        try:
            return str(await self._plugin.ctx.config.get("personality.personality", "") or "").strip()
        except Exception as exc:
            self._plugin.ctx.logger.debug("读取原生 personality 失败: %s", exc)
            return ""

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

    async def _call_creation_llm(self, prompt: str) -> str:
        """调用创作模型；失败返回空串（当前是唯一常规 LLM 调用点）。"""
        cfg = self._plugin.config
        try:
            result = await asyncio.wait_for(
                self._plugin.ctx.llm.generate(
                    prompt,
                    model=cfg.llm.creation_task or "",
                    temperature=cfg.llm.temperature,
                    max_tokens=256,
                ),
                timeout=30,
            )
        except Exception as exc:
            self._plugin.ctx.logger.warning("创作模型调用失败: %s", exc)
            return ""
        # 成本采样（指标 5）：按字符粗估 token，写入 llm_extra_tokens
        telemetry = getattr(self._plugin, "_telemetry", None)
        if telemetry is not None:
            text = ""
            if isinstance(result, dict):
                text = str(result.get("response") or result.get("content") or "")
            tokens_approx = max(1, (len(prompt) + len(text)) // 3)
            telemetry.record_llm_tokens(float(tokens_approx), task="creation")
        if isinstance(result, dict):
            return str(result.get("response") or result.get("content") or "").strip()
        return ""


__all__ = [
    "NarrativeEngine",
    "local_now",
    "parse_clock",
    "routine_phase",
    "mood_by_energy",
    "default_self_state",
    "default_branch_state",
]