"""验收采样（对应 .scratch/narrative-persona/acceptance-dashboard.md 的 5 指标）。

指标名（写入 metrics/*.csv）：
- user_initiated_freq  用户主动发起频率（由谁起头标记）
- dialogue_depth       对话深度（单条消息长度、往返轮次）
- proactive_sent       主动消息发出计数
- proactive_replied    主动消息 30 分钟内被回复计数
- state_diversity      状态多样性（mood 切换等，随快照采集）

成本侧：事件/编年史的额外 LLM token 由各调用点自行 record 到 `llm_extra_tokens`。
"""

from __future__ import annotations

from typing import Any, Optional

from .store import NarrativeStore


class Telemetry:
    """薄封装：只在 telemetry.enabled 时把采样写入 CSV。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._store: NarrativeStore = plugin._store

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