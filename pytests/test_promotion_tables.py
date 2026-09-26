"""晋升机两表（proposals / promotions）建表与读写测试（v0.2.0 批 2 · C4）。

Frozen 范围（ADR-0002 §4）：本批只建表 + 存储层读写，**不含**晋升逻辑本体（批 4）。

- ``proposals``：target / path / proposed_value / confidence / status /
  evidence_refs / contradicts（批 4 追加）/ source_uid / created_ts / updated_ts
- ``promotions``：审计表，applied / rejected / rolled_back 全记，**含旧值新值**
- 人可读留痕另走 chronicle ``kind=promotion``；原 ``slow_change_log.jsonl`` 已作废
- 空库读不炸（批 4 上线前这两张表一直是空的——这正是 R17 计数器要盯的"恒空"风险）

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_promotion_tables.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_promotion_tables.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import _synth_loader

_STORE = _synth_loader.load("services.store")

NarrativeStore = _STORE.NarrativeStore


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="narrative-promo-test-"))


def _columns(db_path: Path, table: str) -> set:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {str(row[1]) for row in rows}


# ─── 建表 ──────────────────────────────────────────────────────


def test_both_tables_are_created():
    store = NarrativeStore(_tmp_dir())
    assert _columns(store._db_path, "proposals")
    assert _columns(store._db_path, "promotions")


def test_proposals_schema_matches_adr():
    """proposals 列必须与 ADR-0002 §4 一一对应。

    ⚠️ 批 4-C4 追加 ``contradicts`` 列（反证引用：用 proposal id 显式引用，
    **不做文本相似度**——HDSI 3.3 的字面归并器「短声明误并、长改写漏并」是
    作者知情未修的漏洞）。旧库经 ``_COLUMN_MIGRATIONS`` 补列，不重建表。
    """
    store = NarrativeStore(_tmp_dir())
    assert _columns(store._db_path, "proposals") == {
        "id",
        "target",
        "path",
        "proposed_value",
        "confidence",
        "status",
        "evidence_refs",
        "contradicts",
        "source_uid",
        "created_ts",
        "updated_ts",
    }


def test_promotions_schema_has_old_and_new_value():
    """审计表必须同时记旧值与新值——否则回滚无从下手。"""
    store = NarrativeStore(_tmp_dir())
    columns = _columns(store._db_path, "promotions")
    assert {"old_value", "new_value", "action", "proposal_id"} <= columns


def test_tables_are_created_on_legacy_db_too():
    """旧库（无这两表）开库后自动补建——CREATE IF NOT EXISTS 天然幂等。"""
    data_dir = _tmp_dir()
    db_path = data_dir / "narrative.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.commit()
    connection.close()

    store = NarrativeStore(data_dir)
    assert _columns(db_path, "proposals")
    assert _columns(db_path, "promotions")


# ─── 空库读不炸（批 4 前的常态） ───────────────────────────────


def test_empty_tables_read_without_error():
    """批 4 上线前两表恒空，读取必须返回空列表而不是抛错。"""
    store = NarrativeStore(_tmp_dir())
    assert store.list_proposals() == []
    assert store.list_promotions() == []


# ─── 写入与读取 ────────────────────────────────────────────────


def test_add_and_list_proposal():
    store = NarrativeStore(_tmp_dir())
    proposal_id = store.add_proposal(
        target="relationship",
        path="relationship.closeness",
        proposed_value="0.6",
        confidence=0.83,
        evidence_refs="chronicle:12,chronicle:15",
        source_uid="123",
    )
    assert proposal_id > 0
    rows = store.list_proposals()
    assert len(rows) == 1
    assert rows[0]["path"] == "relationship.closeness"
    assert abs(rows[0]["confidence"] - 0.83) < 1e-9
    assert rows[0]["status"] == "pending"
    assert rows[0]["source_uid"] == "123"
    assert rows[0]["created_ts"]
    assert rows[0]["updated_ts"]


def test_list_proposals_filters():
    store = NarrativeStore(_tmp_dir())
    store.add_proposal(target="relationship", path="relationship.trust", confidence=0.9)
    store.add_proposal(target="perspective", path="perspective.world_view", confidence=0.85)
    assert len(store.list_proposals(path="relationship.trust")) == 1
    assert len(store.list_proposals(status="pending")) == 2
    assert store.list_proposals(status="applied") == []


def test_update_proposal_status():
    store = NarrativeStore(_tmp_dir())
    proposal_id = store.add_proposal(target="relationship", path="relationship.trust")
    store.update_proposal_status(proposal_id, "applied")
    rows = store.list_proposals()
    assert rows[0]["status"] == "applied"
    assert rows[0]["updated_ts"] >= rows[0]["created_ts"]


def test_append_and_list_promotion():
    store = NarrativeStore(_tmp_dir())
    promotion_id = store.append_promotion(
        action="applied",
        target="relationship",
        path="relationship.trust",
        old_value="0.4",
        new_value="0.6",
        reason="证据链齐备",
        source_uid="123",
        proposal_id=7,
    )
    assert promotion_id > 0
    rows = store.list_promotions()
    assert rows[0]["action"] == "applied"
    assert rows[0]["old_value"] == "0.4"
    assert rows[0]["new_value"] == "0.6"
    assert rows[0]["proposal_id"] == 7


def test_promotion_records_all_three_actions():
    """applied / rejected / rolled_back 全记（回滚靠逆序读本表）。"""
    store = NarrativeStore(_tmp_dir())
    for action in ("applied", "rejected", "rolled_back"):
        store.append_promotion(
            action=action, target="relationship", path="relationship.trust"
        )
    actions = {row["action"] for row in store.list_promotions()}
    assert actions == {"applied", "rejected", "rolled_back"}


def test_promotion_without_proposal_id_is_allowed():
    """反证/回滚可能不挂具体提案（如人工干预），proposal_id 可空。"""
    store = NarrativeStore(_tmp_dir())
    store.append_promotion(action="rolled_back", target="perspective", path="perspective.world_view")
    assert store.list_promotions()[0]["proposal_id"] is None


def test_promotions_filter_by_path():
    store = NarrativeStore(_tmp_dir())
    store.append_promotion(action="applied", target="relationship", path="relationship.trust")
    store.append_promotion(action="applied", target="perspective", path="perspective.life_goals")
    assert len(store.list_promotions(path="perspective.life_goals")) == 1


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
