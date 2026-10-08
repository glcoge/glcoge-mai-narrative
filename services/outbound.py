"""出站环节（深化 C1）：send_service.after_build_message hook 的实现体。

出站时刻/长度采样、主动开口送达确认、轮次与互动配对。壳规则同 inbound。
"""

from __future__ import annotations

from typing import Any, Dict

from .learning.pairs import message_id_of


async def handle_post_send(plugin, **kwargs: Any) -> Dict[str, Any]:
    """bot 出站消息构建完成后：记录出站时刻/长度与对话轮次配对（指标 1/2 的对照侧）。

    挂载点说明（2026-09-08 修复）：曾挂在 ``send_service.before_send``，但其
    载荷没有 stream_id，轮次配对与 ``_last_bot_sent`` 结构上无法工作，且真机
    上 handler 疑似从未被派发（bot_msg_len 上线起 0 条）。``after_build_message``
    载荷含 stream_id，派发点位于发送链路外层 try/except 内，异常不再静默。
    """
    message = kwargs.get("message")
    resolved_stream = str(kwargs.get("stream_id") or kwargs.get("session_id") or "")
    if plugin._telemetry is None:
        return {"action": "continue", "modified_kwargs": kwargs}
    # 触发层追踪：部署后临时调 debug 日志级别，一轮对话即可确认本 hook 是否被派发
    plugin.ctx.logger.debug("narrative outbound: stream=%s", resolved_stream or "-")
    uid = plugin._streams.uid_of(resolved_stream)
    # 主动开口送达确认（2026-09-22）：proactive_sent 记的是"触发"，而触发后
    # 模型可能选择沉默、也可能 reply 工具失败——只有真正出站了才算数，它才是
    # 承接率的真分母，也只有它才会因无人回应而罚冷落。
    if plugin._proactive is not None and plugin._proactive.mark_delivered(
        resolved_stream, plugin._local_now()
    ):
        plugin._telemetry.record("proactive_delivered", 1, user_id=uid, scope="proactive")
    # 出站采样（出站时刻/轮次配对/bot 长度判定下沉 Telemetry，2026-09-13 C5）
    plugin._telemetry.note_outbound(
        stream_id=resolved_stream,
        user_id=uid,
        message=message,
        now=plugin._local_now(),
    )
    # 互动配对（批 4-C2 / R31）：把本轮出站与最近一条待配对入站配成一对并落盘。
    # 只建不消费；落盘失败静默（配对是旁路，绝不拖垮发送链路）。
    if plugin._pairs is not None:
        plugin._pairs.note_outbound(uid, message_id_of(message), plugin._local_now())
    return {"action": "continue", "modified_kwargs": kwargs}
