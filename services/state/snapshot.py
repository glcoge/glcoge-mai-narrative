"""验收采样（对应 .scratch/narrative-persona/acceptance-dashboard.md 的 5 指标）。

指标名（写入 metrics/*.csv）：
- user_initiated_freq  用户主动发起频率（由谁起头标记）
- dialogue_depth       对话深度，按 scope 区分：user_msg_len/bot_msg_len=单条消息长度、
                       rounds=对话往返轮次（v0.1.4 补齐，入站登记+出站 30 分钟内配对成 1 轮）
- proactive_sent         主动开口**触发**计数（漏斗第一层，未确认送达）
- proactive_delivered    主动开口**确认送达**计数（承接率的真分母）
- proactive_undelivered  超窗仍未被确认送达的主动开口（沉默 / 发送失败）
- proactive_trigger_failed  触发被主程序拒绝（流不存在 / 未排队）
- proactive_replied      一次被接住的承接；**value = 延迟分钟数**（连续量）
- state_diversity        状态多样性（mood 切换等，随快照采集）

⚠️ 2026-09-22 口径变更：`proactive_replied` 的 value 从恒 1 改为**延迟分钟**。
用 `count(*)` 统计次数的脚本不受影响；用 `sum(value)` 的脚本会失真（得到的是
总延迟分钟）。30min / 2h / 6h / 16h 等口径一律在分析层对 value 做筛选：
`count(*) where value <= 30`。原 `proactive_replied_24h` 已废弃——它与 30min
口径共用一条判定链，一条用户消息只会落进其中一个分支，导致 24h 分子被系统性
吞掉（A2 报告里的 24% 即由此而来，真实值 78%）。

成本侧：事件/编年史的额外 LLM token 由各调用点自行 record 到 `llm_extra_tokens`。

2026-09-13 体检（C5）：入站/出站的"何时记什么"判定（轮次配对、用户主动发起、
消息长度）自 plugin.py hook 内联逻辑下沉至此——指标口径集中在单一模块，
hook 只剩编排；跟踪状态的更新保持**无条件**（与下沉前一致），仅 CSV 写入受
telemetry.enabled 门控，A/B 对照窗口的采样口径不受影响。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import datetime

from ..message import outbound_text_len
from ..store import NarrativeStore

# 用户消息与上一条 bot 消息的间隔超过该值，视为"用户主动发起"
_USER_INITIATED_GAP_MINUTES = 5
# 入站消息登记后，出站回复在该窗口内到达则配对为一次对话往返（指标 2；
# 与指标 3 的 30 分钟回复窗口保持同一时间尺度）
_ROUND_PAIR_WINDOW_MINUTES = 30
_STAGE_EPOCH = datetime.datetime(1970, 1, 1)


class Telemetry:
    """验收采样：入站/出站采样判定 + CSV 写入（telemetry.enabled 门控）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._store: NarrativeStore = plugin._store
        # stream_id -> 最后一次 bot 发送时刻（用户主动发起判定）
        self._last_bot_sent: Dict[str, datetime.datetime] = {}
        # stream_id -> 最近一次模式会话入站时刻（指标 2 对话轮次配对）
        self._pending_round: Dict[str, datetime.datetime] = {}

    # ─── 入站/出站采样判定（hook 调用，判定逻辑单点在此） ────────

    def is_user_initiated(self, key: str, now: datetime.datetime) -> bool:
        """只读判定：距上一条 bot 消息超过阈值 = 用户主动发起。

        供指标 1（note_inbound）与 share_urge（plugin 入站 hook）共用口径，
        不改动任何跟踪状态。
        """
        last_sent = self._last_bot_sent.get(key, _STAGE_EPOCH)
        return (now - last_sent).total_seconds() / 60 > _USER_INITIATED_GAP_MINUTES

    def note_inbound(
        self,
        stream_id: str,
        user_id: str,
        text: str,
        now: datetime.datetime,
    ) -> None:
        """入站采样：轮次登记 + 用户主动发起判定 + 入站长度（指标 1/2）。"""
        key = stream_id or user_id
        # 指标 2 · 对话轮次：登记本轮入站，待出站回复配对成一次往返（scope=rounds）
        self._pending_round[key] = now

        # 验收指标 1：用户主动发起（距上一条 bot 消息超过阈值）
        if self.is_user_initiated(key, now):
            self.record("user_initiated_freq", 1, user_id=user_id)
        # 验收指标 2：入站消息长度
        self.record("dialogue_depth", value=float(len(text)), user_id=user_id, scope="user_msg_len")

    def note_outbound(
        self,
        stream_id: str,
        user_id: str,
        message: Any,
        now: datetime.datetime,
    ) -> None:
        """出站采样：出站时刻登记 + 轮次配对 + bot 消息长度（指标 1/2 对照侧）。"""
        if stream_id:
            self._last_bot_sent[stream_id] = now
            # 指标 2 · 对话轮次：本条出站若在窗口内接住一次模式会话入站，记一轮往返
            pending_ts = self._pending_round.pop(stream_id, None)
            if pending_ts is not None and (
                (now - pending_ts).total_seconds() <= _ROUND_PAIR_WINDOW_MINUTES * 60
            ):
                # user_id 由调用方（hook）经 StreamRegistry 反查传入，支持按用户拆轮次
                self.record("dialogue_depth", value=1, scope="rounds", user_id=user_id)
        bot_text_len = outbound_text_len(message)
        if bot_text_len is not None:
            self.record("dialogue_depth", value=float(bot_text_len), scope="bot_msg_len")

    def clear_pending_rounds(self) -> None:
        """清空待配对轮次登记（状态重置用；_last_bot_sent 保留，与原 reset 语义一致）。"""
        self._pending_round.clear()

    # ─── 通用采样 ────────────────────────────────────────────────

    def record(
        self,
        name: str,
        value: float = 1,
        user_id: str = "",
        scope: str = "",
    ) -> None:
        """追加一条采样；总开关关闭时静默跳过。"""
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.telemetry.enabled:
            return
        try:
            self._store.append_metric(
                name=name,
                value=float(value),
                user_id=str(user_id or ""),
                scope=str(scope or ""),
            )
        except Exception as exc:  # 采样失败不影响主流程
            self._plugin.ctx.logger.debug("指标采样失败: %s", exc)

    def record_llm_tokens(self, tokens: int, task: str = "creation") -> None:
        """记录剧本相关额外 LLM token（成本指标）。"""
        self.record("llm_extra_tokens", value=float(tokens), scope=f"task:{task}")


__all__ = ["Telemetry"]
