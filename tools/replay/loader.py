"""回放台数据加载。

只做一件事：把真实聊天记录读进来，按会话切分并提供「私有会话专属标记」抽取。
数据路径一律由调用方传入（CLI 参数），**不硬编码、不复制进仓库**。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

BOT_LABEL = "bot"


def load_messages(path: str | Path) -> List[Dict[str, Any]]:
    """读取 MaiBot-export 导出的 messages.jsonl（每行一个 JSON 对象）。"""
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sessions(messages: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按 session_name 归组。"""
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in messages:
        grouped[str(row.get("session_name") or row.get("session_id") or "")].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: str(r.get("ts_short") or ""))
    return dict(grouped)


def _is_bot(row: Dict[str, Any]) -> bool:
    return bool(row.get("is_bot")) or str(row.get("sender_label")) == BOT_LABEL


def user_turns(messages: Iterable[Dict[str, Any]], session_name: str) -> List[Dict[str, Any]]:
    """某会话中「对方」（非 bot）的发言。"""
    return [
        r
        for r in messages
        if str(r.get("session_name")) == session_name and not _is_bot(r)
    ]


def bot_turns(messages: Iterable[Dict[str, Any]], session_name: str) -> List[Dict[str, Any]]:
    """某会话中 bot 的发言（含主动消息，可用 ``is_proactive`` 区分）。"""
    return [
        r for r in messages if str(r.get("session_name")) == session_name and _is_bot(r)
    ]


def participants(messages: Iterable[Dict[str, Any]], session_name: str) -> List[str]:
    """会话里出现的账号标识（QQ 号），bot 除外。"""
    seen = {
        str(r.get("sender_label"))
        for r in messages
        if str(r.get("session_name")) == session_name and not _is_bot(r)
    }
    return sorted(seen)


def _texts(rows: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(r.get("text") or "") for r in rows]


def distinctive_tokens(
    private_texts: Sequence[str],
    other_texts: Sequence[str],
    *,
    min_len: int = 2,
    max_len: int = 4,
    limit: int = 12,
) -> List[str]:
    """抽取出「只出现在私有会话正文里」的连续片段。

    用途：泄露断言不能只靠一个固定词（容易漏网），也不能用通用子串（容易误报）。
    这里用数据驱动方式——取私有会话独有的 2~4 字片段，按长度倒序取前 ``limit`` 个。
    """
    blob_other = "\n".join(other_texts)
    found: Dict[str, None] = {}
    for text in private_texts:
        text = text.strip()
        for size in range(min_len, max_len + 1):
            for start in range(0, max(0, len(text) - size + 1)):
                piece = text[start : start + size]
                if not piece.strip() or piece.strip() != piece:
                    continue
                if piece not in found and piece not in blob_other:
                    found[piece] = None
    # 长片段更具辨识度，优先返回
    return sorted(found, key=lambda s: (-len(s), s))[:limit]
