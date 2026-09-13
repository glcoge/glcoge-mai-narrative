"""uid↔stream 会话注册表：剧本模式私聊的会话学习、持久化与反查。

2026-09-13 体检（C4）：自 plugin.py 纯搬移——uid/stream 双向映射 + kv 持久化
是"主动消息防重启失联"（P0 踩坑 #5 修复）的 owner，归属服务层后 plugin.py
只剩 hook/命令/API 编排，注册表也可脱离插件实例单独测试。
"""

from __future__ import annotations

import logging
from typing import Dict

from .store import NarrativeStore

# stream 映射在 kv 中的 key（单 JSON dict：uid -> stream_id）
_STREAM_MAP_KEY = "stream_map"


class StreamRegistry:
    """剧本模式私聊的 uid↔stream 映射（内存字典 + store kv 持久化）。"""

    def __init__(self, store: NarrativeStore, logger: logging.Logger) -> None:
        self._store = store
        self._logger = logger
        # uid -> 已学到的私聊 stream_id；stream_id -> uid（反向映射用于 hook 判定）
        self._uid_to_stream: Dict[str, str] = {}
        self._stream_to_uid: Dict[str, str] = {}

    def record(self, user_id: str, stream_id: str) -> None:
        """登记 uid<->stream 映射（私聊主动开口与 hook 判定用）。

        同时持久化到 store 的 kv（key=stream_map），解决主动消息依赖内存映射、
        重启后或用户久未私聊时拿不到送达地址而静默停摆的问题。
        """
        if not user_id or not stream_id:
            return
        self._uid_to_stream[user_id] = stream_id
        self._stream_to_uid[stream_id] = user_id
        if self._store is not None:
            try:
                current_map = self._store.get_kv(_STREAM_MAP_KEY) or {}
                current_map[str(user_id)] = str(stream_id)
                self._store.set_kv(_STREAM_MAP_KEY, current_map)
            except Exception as exc:
                self._logger.debug("stream 映射持久化失败: %s", exc)

    def restore(self) -> None:
        """从 store 回填 uid->stream 映射（启动恢复，防重启后主动消息失联）。"""
        if self._store is None:
            return
        try:
            saved = self._store.get_kv(_STREAM_MAP_KEY) or {}
            for user_id, stream_id in saved.items():
                uid = str(user_id)
                sid = str(stream_id)
                if uid and sid:
                    self._uid_to_stream[uid] = sid
                    self._stream_to_uid[sid] = uid
        except Exception as exc:
            self._logger.debug("stream 映射恢复失败: %s", exc)

    def stream_of(self, user_id: str) -> str:
        """查询用户已知的私聊 stream_id。"""
        return self._uid_to_stream.get(user_id, "")

    def uid_of(self, stream_id: str) -> str:
        """反查会话对应的用户 ID（未知会话返回空串）。"""
        return self._stream_to_uid.get(stream_id, "")

    def known_count(self) -> int:
        """已知会话数（状态摘要展示用）。"""
        return len(self._uid_to_stream)

    def clear(self) -> None:
        """清空全部映射（状态重置用；kv 中的 stream_map 由重置的 kv 清空覆盖）。"""
        self._uid_to_stream.clear()
        self._stream_to_uid.clear()


__all__ = ["StreamRegistry"]
