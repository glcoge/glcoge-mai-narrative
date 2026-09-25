"""消息字段访问：从 hook 载荷 dict 提取统一字段（插件内唯一 seam）。

字段规则对齐主程序 ``src/chat/message_receive/message.py``：
- 私聊判定 = ``"group" if message_info.group_info else "private"``
- 正文优先 ``processed_plain_text``，回退 ``raw_message`` 分段数组
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# RESERVED(R9) / OBSERVE：命令消息的**本地正则兜底**。
# 宿主 hook 载荷里的 ``is_command`` 字段不可靠（归档数据中同一形态的消息有时带
# 有时不带），命令一旦被当成对话素材会污染创作层与关系值（"你本人操作"不是
# bot 的生活）。宿主修好后本兜底可删——删前请在回放台确认 is_command 覆盖度。
# 形态：以 / 或 / 的全角变体开头，后接非空白命令名。
_COMMAND_PATTERN = re.compile(r"^\s*[／/]\S")


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


def looks_like_command(text: str) -> bool:
    """本地正则兜底判定"这是命令不是对话"（RESERVED(R9)，宿主修复后可删）。

    只看正文形态（以 ``/`` 开头的命令），不猜平台协议。宿主 ``is_command`` 为真时
    调用方应直接采信，本函数只在**字段缺失/为假**时补一刀。
    """
    return bool(_COMMAND_PATTERN.match(str(text or "")))


def extract_user_id(message: Dict[str, Any]) -> str:
    """从消息中提取用户 ID。"""
    user_id = str(message.get("user_id") or "").strip()
    if user_id:
        return user_id
    msg_info = message.get("message_info") or {}
    user_info = msg_info.get("user_info") or {}
    return str(user_info.get("user_id") or user_info.get("id") or "").strip()


def outbound_text_len(message: Any) -> Optional[int]:
    """从序列化 SessionMessage 的 raw_message 组件列表提取出站文本总长度。

    组件格式（主程序 ``serialize_session_message`` 序列化产物）：
    文本组件为 ``{"type": "text", "data": "<文本>"}``。

    Returns:
        Optional[int]: 文本总长度；message 缺失/非 dict/无文本组件时返回
        ``None``（调用方跳过记录，避免把"取不到文本"记成 0 污染数据）。

    2026-09-13 体检（C2）：自 plugin.py 纯搬移至此——入站/出站的消息字段
    探针统一收敛在 message.py 这一个 seam（规范 §架构层 2）。
    """
    if not isinstance(message, dict):
        return None
    raw_components = message.get("raw_message")
    if not isinstance(raw_components, list):
        return None
    total_len = 0
    has_text = False
    for component in raw_components:
        if isinstance(component, dict) and component.get("type") == "text":
            total_len += len(str(component.get("data") or ""))
            has_text = True
    return total_len if has_text else None


__all__ = [
    "is_private_chat",
    "looks_like_command",
    "message_text",
    "extract_user_id",
    "outbound_text_len",
]
