"""消息字段访问：从 hook 载荷 dict 提取统一字段（插件内唯一 seam）。

字段规则对齐主程序 ``src/chat/message_receive/message.py``：
- 私聊判定 = ``"group" if message_info.group_info else "private"``
- 正文优先 ``processed_plain_text``，回退 ``raw_message`` 分段数组
"""

from __future__ import annotations

from typing import Any, Dict, List


def is_private_chat(message: Dict[str, Any]) -> bool:
    """判断消息是否为私聊。

    真机实测：本代 hook 载荷没有 is_private/chat_type 字段，主程序判定规则为
    ``chat_type = "group" if message_info.group_info else "private"``
    （src/chat/message_receive/message.py:61）——truthy 判断：group_info 非空 dict 才是群聊，
    空 dict / None / 缺失一律私聊；旧字段（is_private/chat_type/scene/detail_type）仅作兜底。
    """
    msg_info = message.get("message_info")
    if isinstance(msg_info, dict):
        group_info = msg_info.get("group_info")
        if group_info:
            return False
        return True
    if message.get("is_private") is True:
        return True
    chat_type = (
        message.get("chat_type")
        or message.get("scene")
        or message.get("detail_type")
        or ""
    )
    return str(chat_type) in ("private", "direct")


def message_text(message: Dict[str, Any]) -> str:
    """提取消息正文。

    真机实测：``raw_message`` 是分段数组（``[{"type":"text","data":...}]``），
    ``processed_plain_text`` 才是拼好的纯文本，优先用它。
    """
    text = str(message.get("processed_plain_text") or "").strip()
    if text:
        return text
    raw = message.get("raw_message")
    if isinstance(raw, list):
        parts: List[str] = []
        for segment in raw:
            if isinstance(segment, dict):
                data = segment.get("data")
                parts.append(str(data) if data is not None else "")
            else:
                parts.append(str(segment))
        return "".join(parts).strip()
    return str(raw or "").strip()


def extract_user_id(message: Dict[str, Any]) -> str:
    """从消息中提取用户 ID。"""
    user_id = str(message.get("user_id") or "").strip()
    if user_id:
        return user_id
    msg_info = message.get("message_info") or {}
    user_info = msg_info.get("user_info") or {}
    return str(user_info.get("user_id") or user_info.get("id") or "").strip()


__all__ = ["is_private_chat", "message_text", "extract_user_id"]
