"""素材隔离列 + 首次迁移通道测试（v0.2.0 批 1 · C2 / B2+B3）。

锁定 ADR-0004「写入打标」这一层在数据层的落实：

- chronicle / events 落库即带 ``source_uid`` / ``audience``（B2）
- **v0.2.0 批 1 前 store.py 没有任何迁移通道**（只有 ``CREATE TABLE IF NOT EXISTS``），
  旧库（无这两列）必须能靠 ``PRAGMA table_info`` 探测 + ALTER 升级到新 schema（R21）
- 存量 ``kind=diary`` 条目补标 ``source_uid=diary``；**life / daily 不补标**（R20，
  用户裁定：不反推、不清洗、不删除）
- ``push_event`` 从 ``branch:{uid}`` 自动推导 source_uid（对话素材天然只对该用户可见）

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_store_audience_columns.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_store_audience_columns.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import _synth_loader

_STORE = _synth_loader.load("services.store")

NarrativeStore = _STORE.NarrativeStore
SOURCE_DIARY = _STORE.SOURCE_DIARY


# 旧库 schema（v0.1.10 及更早的实际形状：无 source_uid / audience）
_LEGACY_CHRONICLE_DDL = (
    "CREATE TABLE chronicle ("
    " id    INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts    TEXT NOT NULL,"
    " scope TEXT NOT NULL,"
    " kind  TEXT NOT NULL,"
    " text  TEXT NOT NULL)"
)
_LEGACY_EVENTS_DDL = (
    "CREATE TABLE events ("
    " id       INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts       TEXT NOT NULL,"
    " scope    TEXT NOT NULL,"
    " kind     TEXT NOT NULL,"
    " bysource TEXT NOT NULL,"
    " declared INTEGER NOT NULL DEFAULT 0)"
)


def _make_legacy_db(data_dir: Path, rows=None) -> Path:
    """在无隔离列的旧库里预置数据，返回 db 路径。"""
    db_path = data_dir / "narrative.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(_LEGACY_CHRONICLE_DDL)
        connection.execute(_LEGACY_EVENTS_DDL)
        for ts, kind, text in rows or []:
            connection.execute(
                "INSERT INTO chronicle (ts, scope, kind, text) VALUES (?, 'self', ?, ?)",
                (ts, kind, text),
            )
        connection.commit()
    finally:
        connection.close()
    return db_path


def _columns(db_path: Path, table: str) -> set:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {str(row[1]) for row in rows}


def _chronicle_rows(db_path: Path) -> list:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT kind, source_uid, audience, text FROM chronicle ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="narrative-store-test-"))


# ─── B2：新库 schema ────────────────────────────────────────────


def test_fresh_db_has_isolation_columns():
    """新建库直接带隔离列（不依赖迁移）。"""
    store = NarrativeStore(_tmp_dir())
    db_path = store._db_path
    assert {"source_uid", "audience"} <= _columns(db_path, "chronicle")
    assert "source_uid" in _columns(db_path, "events")


def test_append_chronicle_persists_explicit_tags():
    """显式打标原样落库。"""
    store = NarrativeStore(_tmp_dir())
    store.append_chronicle("self", "life", "通用片段")
    store.append_chronicle("self", "life", "涉私片段", source_uid="123", audience="123")
    rows = _chronicle_rows(store._db_path)
    assert rows[0] == {"kind": "life", "source_uid": "", "audience": "", "text": "通用片段"}
    assert rows[1] == {
        "kind": "life",
        "source_uid": "123",
        "audience": "123",
        "text": "涉私片段",
    }


def test_append_chronicle_auto_tags_diary():
    """``kind=diary`` 未显式打标时自动补 ``SOURCE_DIARY`` —— 写入打标不靠调用方自觉。"""
    store = NarrativeStore(_tmp_dir())
    store.append_chronicle("self", "diary", "今天的日记")
    rows = _chronicle_rows(store._db_path)
    assert rows[0]["source_uid"] == SOURCE_DIARY
    assert SOURCE_DIARY == "diary"


def test_recent_chronicle_returns_isolation_columns():
    """读取侧要能拿到标签：recent_chronicle 必须回传两列（过滤在 audience.py 做）。"""
    store = NarrativeStore(_tmp_dir())
    store.append_chronicle("self", "life", "甲", source_uid="123", audience="123")
    entry = store.recent_chronicle("self", limit=1)[0]
    assert entry["source_uid"] == "123"
    assert entry["audience"] == "123"
    assert entry["text"] == "甲"


def test_push_event_derives_source_uid_from_branch_scope():
    """对话素材缺省从 ``branch:{uid}`` 反推来源（ADR-0004：只对该用户可见）。"""
    store = NarrativeStore(_tmp_dir())
    store.push_event({"scope": "branch:123", "bysource": "原文"})
    store.push_event({"scope": "self", "bysource": "通用", "source_uid": ""})
    events = store.list_events("branch:123", limit=5)
    assert events[0]["source_uid"] == "123"
    assert store.list_events("self", limit=5)[0]["source_uid"] == ""


def test_push_event_explicit_source_uid_wins():
    """显式 source_uid 覆盖推导。"""
    store = NarrativeStore(_tmp_dir())
    store.push_event({"scope": "branch:123", "bysource": "原文", "source_uid": "456"})
    assert store.list_events("branch:123", limit=5)[0]["source_uid"] == "456"


# ─── B2：旧库迁移（R21，首次建立的迁移通道） ─────────────────────


def test_legacy_db_gets_migrated():
    """旧库（无两列）开库后自动 ALTER 补齐，且**不丢数据**。"""
    data_dir = _tmp_dir()
    _make_legacy_db(data_dir, [("2026-09-21T00:00:00", "diary", "旧日记")])
    store = NarrativeStore(data_dir)
    db_path = store._db_path
    assert {"source_uid", "audience"} <= _columns(db_path, "chronicle")
    assert "source_uid" in _columns(db_path, "events")
    assert _chronicle_rows(db_path)[0]["text"] == "旧日记"


def test_migration_is_idempotent():
    """反复开库不报错、不重复加列（每次开库都跑迁移，故必须幂等）。"""
    data_dir = _tmp_dir()
    _make_legacy_db(data_dir)
    for _ in range(3):
        NarrativeStore(data_dir)
    assert len(_columns(data_dir / "narrative.db", "chronicle")) == 7


# ─── B3：存量打标（R20） ────────────────────────────────────────


def test_legacy_diary_rows_backfilled():
    """存量 diary 条目补标 ``source_uid=diary``（隔离开关，不清洗不删除）。"""
    data_dir = _tmp_dir()
    _make_legacy_db(
        data_dir,
        [
            ("2026-09-20T00:00:00", "diary", "日记一"),
            ("2026-09-21T00:00:00", "diary", "日记二"),
        ],
    )
    NarrativeStore(data_dir)
    rows = _chronicle_rows(data_dir / "narrative.db")
    assert [row["source_uid"] for row in rows] == [SOURCE_DIARY, SOURCE_DIARY]
    assert [row["text"] for row in rows] == ["日记一", "日记二"]  # 未被清洗


def test_legacy_non_diary_rows_not_backfilled():
    """life / daily **不补标**（用户裁定：不反推涉私原文，内测期影响可接受）。"""
    data_dir = _tmp_dir()
    _make_legacy_db(
        data_dir,
        [
            ("2026-09-20T00:00:00", "life", "生活片段"),
            ("2026-09-21T00:00:00", "daily", "每日摘要"),
        ],
    )
    NarrativeStore(data_dir)
    rows = _chronicle_rows(data_dir / "narrative.db")
    assert [row["source_uid"] for row in rows] == ["", ""]


def test_backfill_never_overwrites_existing_tag():
    """迁移只补空值：已打标的条目不被覆盖。"""
    data_dir = _tmp_dir()
    _make_legacy_db(data_dir, [("2026-09-20T00:00:00", "diary", "日记")])
    store = NarrativeStore(data_dir)
    # 模拟"后来人工打标"：写入非 diary 标记后再开一次库
    with store._transaction() as connection:
        connection.execute("UPDATE chronicle SET source_uid = 'manual' WHERE id = 1")
    NarrativeStore(data_dir)
    assert _chronicle_rows(data_dir / "narrative.db")[0]["source_uid"] == "manual"


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
