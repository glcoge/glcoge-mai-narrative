"""回放台数据加载。

只做一件事：把真实聊天记录读进来，按会话切分并提供「私有会话专属标记」抽取。
数据路径一律由调用方传入（CLI 参数），**不硬编码、不复制进仓库**。

批 4-C11 扩展：除 messages.jsonl 外，还要能读**归档的 narrative 库与 metrics csv**
（晋升机定标需要真实 chronicle 与真实正向信号）。
"""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

BOT_LABEL = "bot"

#: chronicle 表的期望列（老归档可能缺 source_uid/audience —— 批 1 才加的）。
_CHRONICLE_COLUMNS = ("id", "ts", "scope", "kind", "text", "source_uid", "audience")


@dataclass(frozen=True)
class ReplayInputs:
    """回放台一次运行的全部外部输入（路径一律由 CLI 传入，不入库）。"""

    messages: str = ""
    db: str = ""
    metrics_dir: str = ""

    def has_db(self) -> bool:
        return bool(self.db) and Path(self.db).exists()

    def has_metrics(self) -> bool:
        return bool(self.metrics_dir) and Path(self.metrics_dir).is_dir()


def load_messages(path: str | Path) -> List[Dict[str, Any]]:
    """读取 MaiBot-export 导出的 messages.jsonl（每行一个 JSON 对象）。"""
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_chronicle(db_path: str | Path) -> List[Dict[str, Any]]:
    """读取归档 ``narrative.db`` 的 chronicle 表。

    ❗ **先复制到临时文件再开库**：这份库是服务器全量归档的一部分，验收过程绝不
    写原库（写一次就没了）。复制用 ``shutil.copy2`` 保留原 mtime，便于事后核对
    原文件未被改动；再以 ``mode=ro`` URI 打开副本，双重保险。

    老归档（v0.1.x）的 chronicle 缺 ``source_uid`` / ``audience``（批 1 才加），
    因此按 **PRAGMA 实际列** 动态取列，缺的补空串——不假设 schema 版本。
    """
    source = Path(db_path)
    if not source.exists():
        raise FileNotFoundError(f"归档库不存在: {source}")
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / (source.name or "archive.db")
        shutil.copy2(source, copy)
        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            connection.row_factory = sqlite3.Row
            present = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(chronicle)")
            }
            if not present:
                return []  # 该库没有 chronicle 表（早期版本） → 视为空，不算失败
            columns = [name for name in _CHRONICLE_COLUMNS if name in present]
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM chronicle ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
    result: List[Dict[str, Any]] = []
    for row in rows:
        item = {name: row[name] for name in columns}
        for name in _CHRONICLE_COLUMNS:
            item.setdefault(name, "")
        result.append(item)
    return result


def load_metrics_csv(metrics_dir: str | Path) -> Dict[str, List[Dict[str, str]]]:
    """读取归档 ``metrics/`` 目录下的全部 ``*.csv``（表头 ts,scope,user_id,value）。"""
    root = Path(metrics_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"归档 metrics 目录不存在: {root}")
    bundle: Dict[str, List[Dict[str, str]]] = {}
    for path in sorted(root.glob("*.csv")):
        with path.open("r", encoding="utf-8") as handle:
            bundle[path.stem] = list(csv.DictReader(handle))
    return bundle


@dataclass
class CsvMetricsStore:
    """把归档 csv 包成 **store 的最小读接口**（只实现 ``read_metrics``）。

    用途：``evidence.positive_signal_days(store, uid)`` 只消费 ``read_metrics``
    ——回放时不建库、不写盘，直接把归档 csv 喂给它（附加禁令 B：判定逻辑单实现，
    回放台绝不复制一份判定规则）。
    """

    bundle: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)

    def read_metrics(self, name: str) -> List[Dict[str, Any]]:
        """读取某指标全部采样（新→旧，与 ``NarrativeStore.read_metrics`` 同口径）。"""
        return list(reversed(self.bundle.get(str(name), [])))

    @classmethod
    def from_dir(cls, metrics_dir: str | Path) -> "CsvMetricsStore":
        return cls(bundle=load_metrics_csv(metrics_dir))



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
