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

# 迟来承接窗口（2026-09-22 issue-bysource-rework P1）：
# 测试者多为学生，可即时回复的窗口很窄，30 分钟口径系统性低估承接率。
# 故并行记录 24 小时内的回复，作为「24h 承接率」指标（与 30min 口径并列看）。
_PROACTIVE_LATE_WINDOW_HOURS = 24
_LATE_KEEP_PER_USER = 10

# share_urge（v0.1.8 第一步）采样未过后的重试间隔范围（分钟）：短延迟重试而非
# 重置完整随机间隔，避免分享欲高时错过整段活跃窗口；也不密集轮询骚扰判定。
# 第一步暂不入 config（对行为影响小），后续按真机数据再定。
_URGE_RETRY_RANGE = (30, 60)

# ISO 星期取值：1=周一 … 7=周日（与 datetime.isoweekday() 对齐）
_VALID_WEEKDAYS = frozenset({"1", "2", "3", "4", "5", "6", "7"})


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


def _in_window(now: datetime.time, start: datetime.time, end: datetime.time) -> bool:
    """单个区间判定，支持跨天（如 20:20-05:00）：右开区间。"""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def in_windows(now: datetime.time, windows: List[str]) -> bool:
    """是否落在任一活跃窗口内（无星期维度，供 default_active_window 使用）。"""
    for window_text in windows:
        window = parse_window(window_text)
        if window is None:
            continue
        if _in_window(now, window[0], window[1]):
            return True
    return False


def rule_matches_now(now: datetime.datetime, rule: Any) -> bool:
    """按用户规则是否命中当前时刻：先判星期，再判时间窗（支持跨天）。

    星期按**当前时刻**判定（2026-09-14 决策）：周五 20:20 起的窗口过了 24:00 即
    失效，不再算作周五规则的延续——否则会污染"周末照常"这类需求。
    ``days`` 为空 → 永不命中（配合"有规则即覆盖默认"实现"永不主动"，修复旧版
    "空列表反而回退默认窗口"的反直觉行为）。
    """
    days = {str(day).strip() for day in (getattr(rule, "days", None) or [])}
    if str(now.isoweekday()) not in days:
        return False
    window = parse_window(f"{getattr(rule, 'start', '')}-{getattr(rule, 'end', '')}")
    if window is None:
        return False
    return _in_window(now.time(), window[0], window[1])


def validate_rules(rules: Any) -> List[str]:
    """校验按用户窗口规则，返回人可读的错误列表（空列表 = 全部合法）。

    用于启动期 WARN：非法条目运行期会被跳过，但必须说清"哪一条、错在哪"——
    静默失效是最贵的失败模式（参考 ``[llm].creation_task`` 教训）。
    """
    errors: List[str] = []
    for index, rule in enumerate(rules or [], start=1):
        user_id = str(getattr(rule, "user_id", "") or "").strip()
        if not user_id.isdigit():
            errors.append(f"第 {index} 条: QQ 号必须是纯数字（当前 {user_id!r}）")
        bad_days = [
            str(day).strip()
            for day in (getattr(rule, "days", None) or [])
            if str(day).strip() not in _VALID_WEEKDAYS
        ]
        if bad_days:
            errors.append(
                f"第 {index} 条: 星期取值非法 {bad_days}（只可填 1-7，1=周一、7=周日）"
            )
        start = str(getattr(rule, "start", "") or "").strip()
        end = str(getattr(rule, "end", "") or "").strip()
        if parse_window(f"{start}-{end}") is None:
            errors.append(
                f"第 {index} 条: 时刻非法（开始 {start!r} / 结束 {end!r}，应为 HH:MM）"
            )
    return errors


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
        # uid -> 主动消息时刻列表（24 小时迟来承接判定，2026-09-22 新增）
        self._sent_long: Dict[str, List[datetime.datetime]] = {}
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
            stream_id = self._plugin._streams.stream_of(user_id)
            if not stream_id:
                continue

            # 被冷落结算（v0.1.8 share_urge）：30 分钟回复窗口过期未回 →
            # 弹出过期记录并按条罚一次（弹出即天然防重复惩罚）。放在窗口/
            # 上限检查之前：即便已超日上限或不在窗口，冷落反馈照样生效。
            expired = self._expire_sent(user_id, now)
            for _ in range(expired):
                self._plugin._engine.record_urge_feedback(user_id, "ignored")

            today = now.strftime("%Y-%m-%d")
            day_count = self._plugin._store.get_kv_int(f"proactive:count:{user_id}:{today}")
            if day_count >= max(0, int(cfg.proactive.daily_max)):
                self._next_fire.pop(user_id, None)
                continue

            if in_silent(now.time(), cfg.proactive.silent_start, cfg.proactive.silent_end):
                self._next_fire.pop(user_id, None)
                continue

            if not self._allowed_now(user_id, now):
                self._next_fire.pop(user_id, None)
                continue

            next_at = self._next_fire.get(user_id)
            if next_at is None:
                low, high = self._random_range()
                self._next_fire[user_id] = now + datetime.timedelta(minutes=random.randint(low, high))
                continue

            if now < next_at:
                continue

            # share_urge 采样（v0.1.8 第一步）：计时器到点只是"最小间隔闸门"，
            # 真正开口还要看此刻想不想说（动机驱动时机）。未过 → 短延迟重试。
            urge = self._plugin._engine.compute_share_urge(user_id)
            if random.random() >= urge:
                low, high = _URGE_RETRY_RANGE
                self._next_fire[user_id] = now + datetime.timedelta(minutes=random.randint(low, high))
                self._plugin.ctx.logger.debug(
                    "主动消息分享欲未过: uid=%s urge=%.2f → %d-%d 分钟后重试",
                    user_id, urge, low, high,
                )
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
        # 迟来承接（24h）：与 30 分钟口径并行，_sent_long 单独保留发送时刻
        long_list = self._sent_long.setdefault(user_id, [])
        long_list.append(now)
        self._sent_long[user_id] = long_list[-_LATE_KEEP_PER_USER:]

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

    def check_late_reply(self, user_id: str, now: datetime.datetime) -> bool:
        """24 小时内收到回复 → 一次"迟来承接"（每条主动消息只计一次）。

        与 30 分钟口径**互不排斥**：同一条消息若已在 30 分钟内被记为"被接住"，
        这里不再重复计数（命中即弹出，天然防重复）。
        """
        entries = self._sent_long.get(user_id, [])
        if not entries:
            return False
        keep: List[datetime.datetime] = []
        hit = False
        for item in entries:
            fresh = (now - item).total_seconds() <= _PROACTIVE_LATE_WINDOW_HOURS * 3600
            if fresh and not hit:
                hit = True  # 弹出该条，后续不再计数
                continue
            if fresh:
                keep.append(item)
            # 超过 24 小时的记录直接丢弃
        self._sent_long[user_id] = keep[-_LATE_KEEP_PER_USER:]
        return hit

    def _expire_sent(self, user_id: str, now: datetime.datetime) -> int:
        """弹出已过 30 分钟回复窗口的发送记录，返回过期条数（每条 = 一次冷落）。

        与 ``check_reply`` 共用 ``_sent_at``：check_reply 只保留窗口内记录用于
        "被接住"判定（由用户回复触发），过期条目的"冷落"惩罚由本方法在
        调度循环里统一结算，两者各取所需、不会重复计数。
        """
        sent_list = self._sent_at.get(user_id, [])
        if not sent_list:
            return 0
        active = [
            item
            for item in sent_list
            if (now - item).total_seconds() / 60 <= _PROACTIVE_REPLY_WINDOW_MINUTES
        ]
        self._sent_at[user_id] = active
        return len(sent_list) - len(active)

    def clear_sent(self) -> None:
        """清空主动消息发送记录（状态重置用）。"""
        self._sent_at.clear()
        self._sent_long.clear()

    # ─── 内部 ────────────────────────────────────────────────────

    def _random_range(self) -> Tuple[int, int]:
        """随机间隔范围（分钟），非法时回退 60-240。"""
        cfg = self._plugin.config
        raw = list(cfg.proactive.random_minutes or [])
        values = [int(item) for item in raw if isinstance(item, (int, float)) and item > 0]
        if len(values) >= 2 and values[0] <= values[1]:
            return values[0], values[1]
        return 60, 240

    def _rules_for(self, user_id: str) -> List[Any]:
        """该用户的窗口规则（可能为空 → 走默认窗口）。"""
        cfg = self._plugin.config
        uid = str(user_id).strip()
        # getattr 兜底：热重载期间可能拿到尚未收敛到新字段的配置对象
        rules = getattr(cfg.proactive, "user_window_rules", None) or []
        return [
            rule
            for rule in rules
            if str(getattr(rule, "user_id", "") or "").strip() == uid
        ]

    def _allowed_now(self, user_id: str, now: datetime.datetime) -> bool:
        """当前时刻是否允许对该用户主动开口。

        - 有按用户规则 → **完全覆盖**默认窗口，任一规则命中即可；全不命中就绝不打扰
          （含"规则存在但 days 为空" = 永不主动）。
        - 无规则 → 退回 ``default_active_window``（无星期维度）。
        """
        rules = self._rules_for(user_id)
        if rules:
            return any(rule_matches_now(now, rule) for rule in rules)
        return in_windows(now.time(), self._plugin.config.proactive.default_active_window or [])


__all__ = [
    "ProactiveScheduler",
    "in_silent",
    "in_windows",
    "parse_window",
    "rule_matches_now",
    "validate_rules",
]