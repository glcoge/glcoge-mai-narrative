"""主动消息调度：活跃窗口 + 随机间隔计时器 + 静默时段 + 每日上限 + 由头签发。

设计共识（grill 会话 Q4）：
- 主动开口必须"有由头"（由头来自剧本状态机，禁止干聊"在吗"）。
- 每用户活跃窗口内用随机区间计时器决定"何时"开口；窗口外/静默期绝不打扰。
- 出站走 Maisaka 原生 proactive 通道，bot 带着剧本上下文完成一轮主动对话。
"""

from __future__ import annotations

import asyncio
import datetime
import random
from typing import Any, Dict, List, Optional, Tuple

from .engine import parse_clock

# 主动消息后 30 分钟内用户回复，记为"被接住"
_PROACTIVE_REPLY_WINDOW_MINUTES = 30


def parse_window(value: str) -> Optional[Tuple[datetime.time, datetime.time]]:
    """解析窗口字符串 ``HH:MM-HH:MM``；失败返回 None。"""
    try:
        start_text, _, end_text = str(value or "").partition("-")
        start = parse_clock(start_text)
        end = parse_clock(end_text)
        if start is None or end is None:
            return None
        return start, end
    except (TypeError, ValueError):
        return None


def in_silent(now: datetime.time, silent_start: str, silent_end: str) -> bool:
    """静默判断：支持跨天区间（如 23:00-08:00）。"""
    start = parse_clock(silent_start)
    end = parse_clock(silent_end)
    if start is None or end is None:
        return False
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def in_windows(now: datetime.time, windows: List[str]) -> bool:
    """是否落在任一活跃窗口内。"""
    for window_text in windows:
        window = parse_window(window_text)
        if window is None:
            continue
        start, end = window
        if start <= end:
            if start <= now < end:
                return True
        else:  # 跨天窗口（如 22:00-02:00）
            if now >= start or now < end:
                return True
    return False


class ProactiveScheduler:
    """主动消息调度器：以 asyncio 周期任务驱动。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # uid -> 下一次开口时刻；窗口外或已用完上限时为 None
        self._next_fire: Dict[str, datetime.datetime] = {}
        # uid -> 最近主动消息时刻列表（30 分钟回复判定）
        self._sent_at: Dict[str, List[datetime.datetime]] = {}
        # stream_id -> 最近主动消息触发时刻（渲染侧判断当前轮是否为主动开口轮）
        self._pending_at: Dict[str, Dict[str, Any]] = {}

    # ─── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """启动调度循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="narrative-proactive")

    async def stop(self) -> None:
        """停止调度循环。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reconcile(self) -> None:
        """按当前配置幂等对齐启停状态（供 plugin 看门狗/配置热重载调用）。"""
        cfg = self._plugin.config
        want = bool(cfg.plugin.enabled and cfg.narrative.enabled and cfg.proactive.enabled)
        if want and not self._running:
            self.start()
        elif not want and self._running:
            await self.stop()

    # ─── 主循环 ──────────────────────────────────────────────────

    async def _loop(self) -> None:
        """每 30 秒检查一次待开口用户。"""
        try:
            while self._running:
                try:
                    await self._check_once()
                except Exception as exc:
                    self._plugin.ctx.logger.error("主动消息调度异常: %s", exc, exc_info=True)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    async def _check_once(self) -> None:
        """单轮检查：窗口/静默/上限 → 是否到点 → 触发。"""
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled or not cfg.proactive.enabled:
            self._next_fire.clear()
            return

        now = self._plugin._local_now()
        for user_id in (cfg.narrative.mode_user_ids or []):
            stream_id = self._plugin._stream_id_of(user_id)
            if not stream_id:
                continue

            today = now.strftime("%Y-%m-%d")
            day_count = self._plugin._store.get_kv_int(f"proactive:count:{user_id}:{today}")
            if day_count >= max(0, int(cfg.proactive.daily_max)):
                self._next_fire.pop(user_id, None)
                continue

            if in_silent(now.time(), cfg.proactive.silent_start, cfg.proactive.silent_end):
                self._next_fire.pop(user_id, None)
                continue

            windows = self._active_windows_for(user_id)
            if not in_windows(now.time(), windows):
                self._next_fire.pop(user_id, None)
                continue

            next_at = self._next_fire.get(user_id)
            if next_at is None:
                low, high = self._random_range()
                self._next_fire[user_id] = now + datetime.timedelta(minutes=random.randint(low, high))
                continue

            if now < next_at:
                continue

            self._next_fire[user_id] = None
            await self._fire(user_id, stream_id, now)

    # ─── 触发 ────────────────────────────────────────────────────

    async def _fire(self, user_id: str, stream_id: str, now: datetime.datetime) -> None:
        """签发由头并触发 Maisaka 主动任务。由头为空（无可借生活素材）则跳过本轮。"""
        cfg = self._plugin.config
        plugin = self._plugin
        bysource = plugin._engine.build_bysource(user_id, now)
        if not bysource:
            plugin.ctx.logger.info(
                "主动消息跳过: uid=%s stream=%s 无可借由的生活素材（不干聊）", user_id, stream_id
            )
            return
        intent = "按剧本生活主动开口"
        plugin.ctx.logger.info(
            "主动消息触发: uid=%s stream=%s 由头=%s", user_id, stream_id, bysource
        )
        try:
            await plugin.ctx.maisaka.proactive.trigger(
                stream_id,
                intent=intent,
                reason=bysource,
                metadata={"source": "glcoge.mai-narrative", "user_id": user_id},
            )
        except Exception as exc:
            plugin.ctx.logger.warning("主动消息触发失败: %s", exc)
            return

        today = now.strftime("%Y-%m-%d")
        day_count = plugin._store.get_kv_int(f"proactive:count:{user_id}:{today}")
        plugin._store.set_kv_int(f"proactive:count:{user_id}:{today}", day_count + 1)
        self.record_sent(user_id, stream_id, now, bysource)
        plugin._telemetry.record("proactive_sent", 1, user_id=user_id, scope="proactive")

    def record_sent(
        self,
        user_id: str,
        stream_id: str,
        now: datetime.datetime,
        bysource: str,
    ) -> None:
        """登记一次主动开口：发送时刻（30 分钟回复判定）+ 侧信道由头（渲染层消费）。"""
        self._sent_at.setdefault(user_id, []).append(now)
        self._pending_at[stream_id] = {
            "ts": now,
            "bysource": bysource,
        }

    def consume_pending(self, session_id: str, window_seconds: int = 30) -> Tuple[str, str]:
        """主动轮判定：本会话 window_seconds 内刚触发过主动消息 → 返回 ("proactive", 由头)。

        触发后由本调度器写入 ``_pending_at``，本处消费一次即清除，避免把后续普通轮次误判为主动轮。

        Returns:
            (round_kind, bysource)：round_kind ∈ {"proactive","reply"}；
            bysource 非空仅当主动轮（由头文本，供渲染注入）。
        """
        entry = self._pending_at.pop(session_id, None)
        if entry is not None and (self._plugin._local_now() - entry["ts"]).total_seconds() <= window_seconds:
            return "proactive", str(entry.get("bysource", "") or "")
        return "reply", ""

    def check_reply(self, user_id: str, now: datetime.datetime) -> bool:
        """主动消息 30 分钟内收到用户回复 → 记一次"被接住"。"""
        sent_list = self._sent_at.get(user_id, [])
        if not sent_list:
            return False
        active = [
            item
            for item in sent_list
            if (now - item).total_seconds() / 60 <= _PROACTIVE_REPLY_WINDOW_MINUTES
        ]
        self._sent_at[user_id] = active[-2:]
        return bool(active)

    def clear_sent(self) -> None:
        """清空主动消息发送记录（状态重置用）。"""
        self._sent_at.clear()

    # ─── 内部 ────────────────────────────────────────────────────

    def _random_range(self) -> Tuple[int, int]:
        """随机间隔范围（分钟），非法时回退 60-240。"""
        cfg = self._plugin.config
        raw = list(cfg.proactive.random_minutes or [])
        values = [int(item) for item in raw if isinstance(item, (int, float)) and item > 0]
        if len(values) >= 2 and values[0] <= values[1]:
            return values[0], values[1]
        return 60, 240

    def _active_windows_for(self, user_id: str) -> List[str]:
        """每用户活跃窗口：优先用户配置，否则默认窗口。"""
        cfg = self._plugin.config
        user_windows = (cfg.proactive.user_active_windows or {}).get(user_id)
        if user_windows:
            return list(user_windows)
        return list(cfg.proactive.default_active_window or [])


__all__ = ["ProactiveScheduler", "in_silent", "in_windows", "parse_window"]