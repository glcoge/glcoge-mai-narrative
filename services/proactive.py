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
        plugin._proactive_sent_at.setdefault(user_id, []).append(now)
        # 侧信道：由头与触发时刻一并暂存，供渲染层注入"主动轮指示 + 由头文本"
        plugin._proactive_pending_at[stream_id] = {
            "ts": now,
            "bysource": bysource,
        }
        plugin._telemetry.record("proactive_sent", 1, user_id=user_id, scope="proactive")

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