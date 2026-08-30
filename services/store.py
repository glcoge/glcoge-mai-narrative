"""叙事状态存储：双层状态机 + 编年史 + 事件队列 + 每日快照 + 指标 CSV。

独立于 MaiBot.db 核心表（规避核心升级迁移风险），落在插件标准数据目录：
``data/plugins/glcoge.mai-narrative/``

- narrative.db（sqlite）：kv 状态（自我层/支线层）、编年史、事件队列
- snapshots/YYYY-MM-DD.json：每日状态快照（变化留痕、回滚点）
- metrics/*.csv：验收采样（含表头，追加写入）
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    """当前本地时间的 ISO 字符串。"""
    return datetime.now().isoformat(timespec="seconds")


class NarrativeStore:
    """封装叙事状态的全部持久化操作。所有方法同步、轻量、可在线程池调用。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._db_path = data_dir / "narrative.db"
        self._snapshots_dir = data_dir / "snapshots"
        self._metrics_dir = data_dir / "metrics"
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        """打开独立连接（写库频率极低，每次操作独立连接已足够）。"""
        connection = sqlite3.connect(self._db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        """初始化表结构。"""
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chronicle (
                    id    INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts    TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    kind  TEXT NOT NULL,
                    text  TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts       TEXT NOT NULL,
                    scope    TEXT NOT NULL,
                    kind     TEXT NOT NULL,
                    bysource TEXT NOT NULL,
                    declared INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chronicle_scope_ts ON chronicle(scope, ts)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_scope_ts ON events(scope, ts)"
            )

    # ─── KV 状态 ────────────────────────────────────────────────

    def get_kv(self, key: str) -> Optional[Dict[str, Any]]:
        """读取 JSON 化 kv 状态；不存在时返回 None。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(str(row["value"]))
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def set_kv(self, key: str, value: Dict[str, Any]) -> None:
        """写入 JSON 化 kv 状态。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_kv_int(self, key: str, default: int = 0) -> int:
        """读取整数型 kv 计数。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return default

    def set_kv_int(self, key: str, value: int) -> None:
        """写入整数型 kv 计数。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def delete_keys_with_prefix(self, prefix: str) -> int:
        """删除 key 以指定前缀开头的全部记录（用于状态重置）。"""
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM kv WHERE key LIKE ?",
                (f"{prefix}%",),
            )
        return int(cursor.rowcount or 0)

    # ─── 编年史（append-only） ───────────────────────────────────

    def append_chronicle(
        self,
        scope: str,
        kind: str,
        text: str,
        ts: Optional[str] = None,
    ) -> None:
        """追加一条编年史条目。历史不可修改、不可删除（append-only）。"""
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO chronicle (ts, scope, kind, text) VALUES (?, ?, ?, ?)",
                (ts or _now_iso(), scope, kind, normalized_text),
            )

    def recent_chronicle(self, scope: str, limit: int = 5) -> List[Dict[str, str]]:
        """读取指定作用域最近的编年史条目。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ts, scope, kind, text FROM chronicle "
                "WHERE scope = ? ORDER BY ts DESC LIMIT ?",
                (scope, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_chronicle(self, scope: str) -> int:
        """统计指定作用域的编年史条目数。"""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS cnt FROM chronicle WHERE scope = ?",
                (scope,),
            ).fetchone()
        return int(row["cnt"] or 0) if row is not None else 0

    def has_chronicle_on_date(self, scope: str, kind: str, date: str) -> bool:
        """指定作用域 + 类型在某个日期是否已有编年史条目（幂等判定用）。

        ``ts`` 存储为 ISO 字符串（``YYYY-MM-DDTHH:MM:SS``），按 ``date%`` 前缀匹配。
        """
        normalized = str(date or "").strip()
        if not normalized:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM chronicle WHERE scope = ? AND kind = ? "
                "AND ts LIKE ? LIMIT 1",
                (scope, kind, f"{normalized}%"),
            ).fetchone()
        return row is not None

    def is_chronicle_done(self, scope: str, kind: str, date: str) -> bool:
        """读取幂等标记（kv: ``chronicle:{scope}:{kind}:{date}``）。"""
        return self.get_kv_int(f"chronicle:{scope}:{kind}:{date}") > 0

    def mark_chronicle_done(self, scope: str, kind: str, date: str) -> None:
        """写入幂等标记（与 append 同事务语义由调用方保证）。"""
        self.set_kv_int(f"chronicle:{scope}:{kind}:{date}", 1)

    def append_chronicle_once(self, scope: str, kind: str, text: str, date: str) -> bool:
        """幂等追加一条编年史：同作用域 + 类型 + 日期已存在则跳过。

        Returns:
            bool: True=本次已写入；False=重复（或文本为空），未写入。
        """
        normalized = str(text or "").strip()
        if not normalized:
            return False
        if self.has_chronicle_on_date(scope, kind, date):
            return False
        # ts 对齐到所属日期当天开始，保证按 date 前缀可幂等匹配
        self.append_chronicle(scope, kind, normalized, ts=f"{date}T00:00:00")
        self.mark_chronicle_done(scope, kind, date)
        return True

    # ─── 事件队列 ───────────────────────────────────────────────

    def push_event(self, event: Dict[str, Any]) -> None:
        """入队一条事件（由头签发器的原料）。"""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO events (ts, scope, kind, bysource, declared) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.get("ts") or _now_iso(),
                    str(event.get("scope", "")),
                    str(event.get("kind", "dialogue_material")),
                    str(event.get("bysource", "")),
                    int(bool(event.get("declared", False))),
                ),
            )

    def list_events(self, scope: str, limit: int = 20) -> List[Dict[str, Any]]:
        """列出指定作用域的事件（新→旧）。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, ts, scope, kind, bysource, declared FROM events "
                "WHERE scope = ? ORDER BY ts DESC LIMIT ?",
                (scope, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_events(self, scope: str) -> None:
        """清空指定作用域的事件队列（已使用过的事件出队）。"""
        with self._connect() as connection:
            connection.execute("DELETE FROM events WHERE scope = ?", (scope,))

    def clear_events_before(self, scope: str, ts: str) -> None:
        """清理指定作用域早于 ts 的事件（事件队列有界）。"""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM events WHERE scope = ? AND ts < ?",
                (scope, ts),
            )

    def clear_all_events(self) -> None:
        """清空全部事件队列（状态重置用）。"""
        with self._connect() as connection:
            connection.execute("DELETE FROM events")

    # ─── 每日快照 ───────────────────────────────────────────────

    def save_snapshot(self, date: str, payload: Dict[str, Any]) -> None:
        """保存每日状态快照（回滚点 + 验收指标 4 的原料）。"""
        snapshot_path = self._snapshots_dir / f"{date}.json"
        snapshot_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_snapshots(self) -> List[str]:
        """列出全部快照日期（新→旧）。"""
        return sorted(
            (path.stem for path in self._snapshots_dir.glob("*.json")),
            reverse=True,
        )

    # ─── 验收指标 CSV ───────────────────────────────────────────

    def append_metric(
        self,
        name: str,
        value: float,
        user_id: str = "",
        scope: str = "",
        ts: Optional[str] = None,
    ) -> None:
        """追加一条指标采样（表头：ts,scope,user_id,value）。"""
        metric_path = self._metrics_dir / f"{name}.csv"
        header = ["ts", "scope", "user_id", "value"]
        is_new = not metric_path.exists()
        with metric_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(header)
            writer.writerow([ts or _now_iso(), scope, user_id, value])

    def read_metrics(self, name: str) -> List[Dict[str, Any]]:
        """读取某指标全部采样（新→旧）。"""
        metric_path = self._metrics_dir / f"{name}.csv"
        if not metric_path.exists():
            return []
        with metric_path.open("r", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return list(reversed(rows))


__all__ = ["NarrativeStore"]