"""叙事状态存储：双层状态机 + 编年史 + 事件队列 + 每日快照 + 指标 CSV。

独立于 MaiBot.db 核心表（规避核心升级迁移风险），落在插件标准数据目录：
``data/plugins/glcoge.mai-narrative/``

- narrative.db（sqlite）：kv 状态（自我层/支线层）、编年史、事件队列
- snapshots/YYYY-MM-DD.json：每日状态快照（变化留痕、回滚点）
- metrics/*.csv：验收采样（含表头，追加写入）

素材隔离列（ADR-0004）：编年史与事件队列**落库即带** ``source_uid`` / ``audience``，
读取侧按受众过滤（过滤规则表单一实现见 ``services/render/audience.py``）。

⚠ v0.2.0 批 1 之前本模块**没有任何迁移通道**（只有 ``CREATE TABLE IF NOT EXISTS``），
旧库升级无处下手。批 1 首次建立 ``_migrate()``：靠 ``PRAGMA table_info`` 探测列存在性
再 ALTER，新旧库都能开。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


def _now_iso() -> str:
    """当前本地时间的 ISO 字符串。"""
    return datetime.now().isoformat(timespec="seconds")


def _uid_from_branch_scope(scope: str) -> str:
    """从支线作用域名反推用户号：``branch:123`` → ``123``；非支线作用域返回空串。

    对话素材的 scope 即来源，写入侧据此自动打标（调用方不必每次都传 ``source_uid``）。
    """
    normalized = str(scope or "")
    prefix = "branch:"
    if normalized.startswith(prefix):
        return normalized[len(prefix) :].strip()
    return ""


#: diary 产物的来源标记（ADR-0004「diary 产物完全隔离」）。
#: 带此标记的条目**永不被读出**（过滤在读取侧执行）；同时是 ``kind="diary"``
#: 写入时的默认 ``source_uid``，使「写入打标」这一层不依赖调用方自觉。
SOURCE_DIARY = "diary"

#: 列级迁移清单（R21）。元组 = (表名, 列名, 追加列的 DDL)。
#: 按声明顺序执行；每次开库都跑，靠 ``PRAGMA table_info`` 探测保证幂等。
#: 新增列只往后追加，**不删不改**历史条目（旧库数据在升级后必须仍可读）。
_COLUMN_MIGRATIONS: List[Tuple[str, str, str]] = [
    (
        "chronicle",
        "source_uid",
        "ALTER TABLE chronicle ADD COLUMN source_uid TEXT NOT NULL DEFAULT ''",
    ),
    (
        "chronicle",
        "audience",
        "ALTER TABLE chronicle ADD COLUMN audience TEXT NOT NULL DEFAULT ''",
    ),
    (
        "events",
        "source_uid",
        "ALTER TABLE events ADD COLUMN source_uid TEXT NOT NULL DEFAULT ''",
    ),
]

#: 列级**删除**清单（批 2，ADR-0002 §8 死字段处决）。元组 = (表名, 列名)。
#: 与 ``_COLUMN_MIGRATIONS`` 分开，因为语义相反：这里真的 DROP，不留兼容冗余。
#: ``ALTER TABLE ... DROP COLUMN`` 需 SQLite ≥ 3.35（本机实测 3.50.4 ✅）。
_DROPPED_COLUMNS: List[Tuple[str, str]] = [
    # events.declared：写入恒 0、全仓从不读取的死列（全量架构解析报告 §3.7）
    ("events", "declared"),
]


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
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        """打开独立连接（写库频率极低，每次操作独立连接已足够）。"""
        connection = sqlite3.connect(self._db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """独立连接 + 事务语义 + **用毕关闭**。

        2026-09-13 体检发现：原先所有方法 ``with self._connect() as connection``
        的写法只提交不关闭（sqlite3.Connection 上下文仅 commit/rollback），
        连接依赖 GC 回收——Windows 上文件句柄不即时释放，narrative.db 会被
        锁住（备份/复制失败）。统一走本入口：commit 语义不变，退出即关闭。
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        """初始化表结构。"""
        with self._transaction() as connection:
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
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT NOT NULL,
                    scope      TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    text       TEXT NOT NULL,
                    source_uid TEXT NOT NULL DEFAULT '',
                    audience   TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT NOT NULL,
                    scope      TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    bysource   TEXT NOT NULL,
                    source_uid TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chronicle_scope_ts ON chronicle(scope, ts)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_scope_ts ON events(scope, ts)"
            )
            # 晋升机两表（批 2，ADR-0002 §4）。此批只建表与审计读写，
            # 晋升逻辑本体属批 4——schema 一次做完，免得批 4 又动 store。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS proposals (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    target         TEXT NOT NULL,
                    path           TEXT NOT NULL,
                    proposed_value TEXT NOT NULL DEFAULT '',
                    confidence     REAL NOT NULL DEFAULT 0.0,
                    status         TEXT NOT NULL DEFAULT 'pending',
                    evidence_refs  TEXT NOT NULL DEFAULT '',
                    source_uid     TEXT NOT NULL DEFAULT '',
                    created_ts     TEXT NOT NULL,
                    updated_ts     TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS promotions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id INTEGER,
                    action      TEXT NOT NULL,
                    target      TEXT NOT NULL,
                    path        TEXT NOT NULL,
                    old_value   TEXT NOT NULL DEFAULT '',
                    new_value   TEXT NOT NULL DEFAULT '',
                    reason      TEXT NOT NULL DEFAULT '',
                    source_uid  TEXT NOT NULL DEFAULT '',
                    ts          TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, path)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_promotions_path ON promotions(path, ts)"
            )

    # ─── 迁移（v0.2.0 批 1 首次建立，R21） ────────────────────────

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set:
        """读取某表现有列名集合（``PRAGMA table_info``）。

        表名取自 ``_COLUMN_MIGRATIONS`` 常量、非外部输入，故此处拼 f-string 安全。
        """
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def _migrate(self) -> None:
        """把**旧库**补齐到当前 schema：缺列则 ALTER，并对存量 diary 条目打标。

        幂等性靠两条保证，因此每次开库都跑一遍是安全的：
        1. 列存在性用 ``PRAGMA table_info`` 探测，已有则跳过；
        2. 存量打标带 ``source_uid = ''`` 守卫，只补空值，不覆盖历史标签。

        只增列、只补空值：**从不删改既有行**（升级后旧数据必须仍可读）。

        批 2 追加一步**列删除**（``_DROPPED_COLUMNS``）：死列真删，不留兼容冗余。
        新库建表时已不含该列，探测不到即跳过——所以本步只对旧库生效。
        """
        with self._transaction() as connection:
            for table, column, ddl in _COLUMN_MIGRATIONS:
                if column not in self._table_columns(connection, table):
                    connection.execute(ddl)
            for table, column in _DROPPED_COLUMNS:
                if column in self._table_columns(connection, table):
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            # 存量打标（R20）：既有 diary 条目按「完全隔离」处理，不清洗不删除。
            # 只补空值——用户已裁定 life/daily 不做反推打标（涉私影响内测期可接受），
            # 故此处**不碰**非 diary 条目，也不动它们的 source_uid。
            connection.execute(
                "UPDATE chronicle SET source_uid = ?, audience = '' "
                "WHERE kind = ? AND (source_uid IS NULL OR source_uid = '')",
                (SOURCE_DIARY, "diary"),
            )

    # ─── KV 状态 ────────────────────────────────────────────────

    def get_kv(self, key: str) -> Optional[Dict[str, Any]]:
        """读取 JSON 化 kv 状态；不存在时返回 None。"""
        with self._transaction() as connection:
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
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def get_kv_int(self, key: str, default: int = 0) -> int:
        """读取整数型 kv 计数。"""
        with self._transaction() as connection:
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
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_kv_str(self, key: str, default: str = "") -> str:
        """读取字符串型 kv 值（时间戳等，不再用 dict 包装）。"""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_kv_str(self, key: str, value: str) -> None:
        """写入字符串型 kv 值。"""
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_kv_with_prefix(self, prefix: str) -> Dict[str, Dict[str, Any]]:
        """按 key 前缀批量读取 JSON 化 kv（话题权重等聚合项用，批 3-R16）。

        损坏的 JSON 值静默跳过：聚合项丢一条不影响整体，但不该让读取整体炸掉。
        """
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT key, value FROM kv WHERE key LIKE ?",
                (f"{prefix}%",),
            ).fetchall()
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(str(row["value"]))
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                result[str(row["key"])] = value
        return result

    def delete_keys_with_prefix(self, prefix: str) -> int:
        """删除 key 以指定前缀开头的全部记录（用于状态重置）。"""
        with self._transaction() as connection:
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
        source_uid: str = "",
        audience: str = "",
    ) -> None:
        """追加一条编年史条目。历史不可修改、不可删除（append-only）。

        写入打标（ADR-0004 第 1 层）：``source_uid`` 记素材来源、``audience`` 记可见受众。
        两者缺省为空串＝**通用素材**（全用户可见）。``kind="diary"`` 且未显式打标时
        自动补 ``SOURCE_DIARY``——「写入打标」这一层不依赖调用方自觉。
        """
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        normalized_source = str(source_uid or "").strip()
        if kind == "diary" and not normalized_source:
            normalized_source = SOURCE_DIARY
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO chronicle (ts, scope, kind, text, source_uid, audience) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ts or _now_iso(),
                    scope,
                    kind,
                    normalized_text,
                    normalized_source,
                    str(audience or "").strip(),
                ),
            )

    def recent_chronicle(self, scope: str, limit: int = 5) -> List[Dict[str, str]]:
        """读取指定作用域最近的编年史条目（**不做受众过滤**）。

        返回条目含 ``source_uid`` / ``audience`` 两列，供读取侧过滤。过滤规则表
        单一实现在 ``services/render/audience.py``（ADR-0004 第 2 层），本方法刻意
        保持"哑数据层"——否则规则会散落在 SQL 与 Python 两处。
        """
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT ts, scope, kind, text, source_uid, audience FROM chronicle "
                "WHERE scope = ? ORDER BY ts DESC LIMIT ?",
                (scope, max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_chronicle(self, scope: str) -> int:
        """统计指定作用域的编年史条目数。"""
        with self._transaction() as connection:
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
        with self._transaction() as connection:
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

    def append_chronicle_once(
        self,
        scope: str,
        kind: str,
        text: str,
        date: str,
        source_uid: str = "",
        audience: str = "",
    ) -> bool:
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
        self.append_chronicle(
            scope,
            kind,
            normalized,
            ts=f"{date}T00:00:00",
            source_uid=source_uid,
            audience=audience,
        )
        self.mark_chronicle_done(scope, kind, date)
        return True

    # ─── 事件队列 ───────────────────────────────────────────────

    def push_event(self, event: Dict[str, Any]) -> None:
        """入队一条事件（由头签发器的原料）。

        写入打标（ADR-0004 第 1 层）：``source_uid`` 缺省时从 ``scope`` 反推
        （``branch:{uid}`` → ``uid``），使对话素材天然只对该用户可见。
        """
        source_uid = str(event.get("source_uid", "") or "").strip()
        if not source_uid:
            source_uid = _uid_from_branch_scope(str(event.get("scope", "")))
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO events (ts, scope, kind, bysource, source_uid) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    event.get("ts") or _now_iso(),
                    str(event.get("scope", "")),
                    str(event.get("kind", "dialogue_material")),
                    str(event.get("bysource", "")),
                    source_uid,
                ),
            )

    def list_events(self, scope: str, limit: int = 20) -> List[Dict[str, Any]]:
        """列出指定作用域的事件（新→旧）。与 ``recent_chronicle`` 同口径：不做受众过滤。"""
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, ts, scope, kind, bysource, source_uid FROM events "
                "WHERE scope = ? ORDER BY ts DESC LIMIT ?",
                (scope, max(1, min(limit, 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_events(self, scope: str) -> None:
        """清空指定作用域的事件队列（已使用过的事件出队）。"""
        with self._transaction() as connection:
            connection.execute("DELETE FROM events WHERE scope = ?", (scope,))

    def clear_events_before(self, scope: str, ts: str) -> None:
        """清理指定作用域早于 ts 的事件（事件队列有界）。"""
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM events WHERE scope = ? AND ts < ?",
                (scope, ts),
            )

    def clear_all_events(self) -> None:
        """清空全部事件队列（状态重置用）。"""
        with self._transaction() as connection:
            connection.execute("DELETE FROM events")

    # ─── 晋升机两表（批 2 建表 / 批 4 写逻辑，ADR-0002 §4） ─────────

    def add_proposal(
        self,
        *,
        target: str,
        path: str,
        proposed_value: str = "",
        confidence: float = 0.0,
        status: str = "pending",
        evidence_refs: str = "",
        source_uid: str = "",
    ) -> int:
        """登记一条慢变提案，返回自增 id。

        ``path`` 必须是慢变白名单路径——调用方（批 4）负责校验；store 不做白名单
        判定，保持"存储层不认识业务规则"的分层（同 audience 过滤的分层原则）。
        """
        now = _now_iso()
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO proposals "
                "(target, path, proposed_value, confidence, status, evidence_refs, "
                " source_uid, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(target),
                    str(path),
                    str(proposed_value),
                    float(confidence),
                    str(status),
                    str(evidence_refs),
                    str(source_uid),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_proposals(
        self, path: str = "", status: str = "", limit: int = 50
    ) -> List[Dict[str, Any]]:
        """列出提案（新→旧），可按 path / status 过滤。"""
        clauses: List[str] = []
        params: List[Any] = []
        if path:
            clauses.append("path = ?")
            params.append(str(path))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, target, path, proposed_value, confidence, status, "
                "evidence_refs, source_uid, created_ts, updated_ts "
                f"FROM proposals {where} ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_proposal_status(self, proposal_id: int, status: str) -> None:
        """更新提案状态（pending → applied / rejected / rolled_back）。"""
        with self._transaction() as connection:
            connection.execute(
                "UPDATE proposals SET status = ?, updated_ts = ? WHERE id = ?",
                (str(status), _now_iso(), int(proposal_id)),
            )

    def append_promotion(
        self,
        *,
        action: str,
        target: str,
        path: str,
        old_value: str = "",
        new_value: str = "",
        reason: str = "",
        source_uid: str = "",
        proposal_id: Optional[int] = None,
        ts: str = "",
    ) -> int:
        """写一条晋升审计（applied / rejected / rolled_back 全记，含旧值新值）。

        与 chronicle ``kind=promotion`` 人可读留痕互补：这里记机器可回滚的旧值新值。
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO promotions "
                "(proposal_id, action, target, path, old_value, new_value, reason, "
                " source_uid, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(proposal_id) if proposal_id is not None else None,
                    str(action),
                    str(target),
                    str(path),
                    str(old_value),
                    str(new_value),
                    str(reason),
                    str(source_uid),
                    ts or _now_iso(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def list_promotions(self, path: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """列出晋升审计（新→旧）；回滚按本表逆序恢复旧值（批 4）。"""
        params: List[Any] = []
        where = ""
        if path:
            where = "WHERE path = ?"
            params.append(str(path))
        params.append(max(1, min(int(limit), 500)))
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, proposal_id, action, target, path, old_value, new_value, "
                f"reason, source_uid, ts FROM promotions {where} ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

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


__all__ = ["NarrativeStore", "SOURCE_DIARY"]