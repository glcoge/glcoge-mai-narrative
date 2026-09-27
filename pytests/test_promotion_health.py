"""晋升健康度诊断（v0.2.0 后 · A 主线）：区分「在等时机」与「在空转」。

背景：晋升机是确定性状态机，门槛在**归档数据**上定标。上线后它若悄悄不再产出，
外表与「正常但在等时机」完全一样 —— HDSI 5.9「恒空」就是这么发生的。
``services/learning/health.py`` 把两者分开，本文件锁住它的行为。

最关键的一条是**判据同源**：诊断绝不能自己写一份门槛。第 1 条用例用参数化穷举
把诊断的 ``blocking`` 与晋升机的 ``meets_minor_gate`` 对撞，任何一处改了判据而
另一处没跟，这条立刻红。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_promotion_health.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_promotion_health.py
"""

from __future__ import annotations

import datetime
import sys
import tempfile
import types
from pathlib import Path

import _synth_loader

_CONT = _synth_loader.load("services.state.continuity")
_STORE = _synth_loader.load("services.store")
_HEALTH = _synth_loader.load("services.learning.health")

promotion_health = _HEALTH.promotion_health
minor_gate_checks = _CONT.minor_gate_checks
major_gate_checks = _CONT.major_gate_checks
meets_minor_gate = _CONT.meets_minor_gate
meets_major_gate = _CONT.meets_major_gate
NarrativeStore = _STORE.NarrativeStore

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0)
DAY = datetime.timedelta(days=1)


def _config(**overrides):
    return _synth_loader.promotion_config(**overrides)


def _make_store(tmp: str) -> NarrativeStore:
    return NarrativeStore(Path(tmp))


def _seed_days(store: NarrativeStore, days: int, *, per_day: int = 1) -> str:
    """造 ``days`` 个自然日、每天 ``per_day`` 条 life 素材；返回引用串。"""
    refs: list = []
    for index in range(days):
        ts = (NOW - datetime.timedelta(days=index)).isoformat(timespec="seconds")
        for slot in range(per_day):
            store.append_chronicle("self", "life", f"素材{index}-{slot}", ts=ts)
    rows = store.list_chronicle_rows("self", limit=500)
    for row in rows:
        refs.append(f"chronicle:{row['id']}")
    return ",".join(refs)


def _add_proposal(
    store: NarrativeStore, *, path: str, confidence: float, refs: str
) -> int:
    return store.add_proposal(
        target="perspective",
        path=path,
        proposed_value="测试值",
        confidence=confidence,
        status="pending",
        evidence_refs=refs,
    )


# ===== 1. 判据同源（最重要） =====


def test_gate_checks_match_meets_functions():
    """门槛的逐项判定与 ``meets_*`` 必须同源（``meets_*`` 是它的 all 折叠）。"""
    cases = [
        (0.9, 5, 5),
        (0.82, 3, 2),
        (0.81, 3, 2),
        (0.9, 2, 9),
        (0.9, 9, 1),
        (0.5, 9, 9),
    ]
    for confidence, scenes, days in cases:
        checks = minor_gate_checks(
            confidence=confidence,
            scene_count=scenes,
            day_count=days,
            min_confidence=0.82,
            min_scenes=3,
            min_days=2,
        )
        expected = meets_minor_gate(
            confidence=confidence,
            scene_count=scenes,
            day_count=days,
            min_confidence=0.82,
            min_scenes=3,
            min_days=2,
        )
        assert all(checks.values()) is expected, (
            f"逐项判定与 meets_minor_gate 不一致: {confidence}/{scenes}/{days}"
        )
    for confidence, scenes in [(0.95, 2), (0.94, 9), (0.99, 1)]:
        checks = major_gate_checks(
            confidence=confidence,
            scene_count=scenes,
            min_confidence=0.95,
            min_scenes=2,
        )
        expected = meets_major_gate(
            confidence=confidence,
            scene_count=scenes,
            min_confidence=0.95,
            min_scenes=2,
        )
        assert all(checks.values()) is expected


def test_diagnosis_blocking_agrees_with_gate():
    """诊断的 ``blocking`` 必须等于晋升机的门槛结论（穷举对撞，防第二份判据）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        refs = _seed_days(store, 5)

        for confidence, scenes, days in [
            (0.9, 5, 5),
            (0.82, 3, 2),
            (0.81, 3, 2),
            (0.9, 2, 9),
            (0.9, 9, 1),
            (0.5, 9, 9),
        ]:
            _add_proposal(
                store,
                path="perspective.world_view",
                confidence=confidence,
                refs=refs if scenes >= 3 else _seed_days(store, scenes),
            )
            report = promotion_health(store, _config(), now=NOW)
            item = report["pending"][0]
            gate_ok = meets_minor_gate(
                confidence=confidence,
                scene_count=item["scene_count"],
                day_count=item["day_count"],
                min_confidence=0.82,
                min_scenes=3,
                min_days=2,
            )
            # 不在冷却时：过门槛 → blocking 为 None；未过 → 一定是 "gate"
            assert (item["blocking"] is None) is gate_ok, (
                f"诊断与晋升机判定不一致: {confidence}/{scenes}/{days} "
                f"-> blocking={item['blocking']} gate_ok={gate_ok}"
            )
            store.update_proposal_status(item["id"], "rejected")


# ===== 2. 四种结论 =====


def test_verdict_cooling():
    """有提案但全部卡冷却 → cooling（设计内，不是空转）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        refs = _seed_days(store, 5)
        _add_proposal(store, path="perspective.world_view", confidence=0.9, refs=refs)
        store.set_kv_str(
            "promotion:cooldown:perspective.world_view",
            (NOW - datetime.timedelta(hours=1)).isoformat(timespec="seconds"),
        )

        report = promotion_health(store, _config(), now=NOW)
        assert report["verdict"] == "cooling"
        assert report["pending"][0]["blocking"] == "cooldown"
        locked = [
            item
            for item in report["cooldowns"]
            if item["path"] == "perspective.world_view"
        ]
        assert locked and locked[0]["blocked"] is True
        assert locked[0]["hours_left"] > 0


def test_verdict_below_gate_lists_failed_items():
    """未过门槛 → below_gate，且 failed 精确列出未过的项。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        # 只造 1 天 1 条 → 场景 1 < 3、跨日 1 < 2，置信也不够
        refs = _seed_days(store, 1)
        _add_proposal(store, path="perspective.world_view", confidence=0.5, refs=refs)

        report = promotion_health(store, _config(), now=NOW)
        assert report["verdict"] == "below_gate"
        item = report["pending"][0]
        assert item["blocking"] == "gate"
        assert set(item["failed"]) == {"confidence", "scenes", "days"}


def test_verdict_ready():
    """过全部门槛且不在冷却 → ready（下一个 tick 就该晋升）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        refs = _seed_days(store, 4)
        _add_proposal(store, path="perspective.world_view", confidence=0.9, refs=refs)

        report = promotion_health(store, _config(), now=NOW)
        assert report["verdict"] == "ready"
        assert report["pending"][0]["blocking"] is None
        assert report["pending"][0]["failed"] == []


def test_verdict_no_proposals():
    """没有待决提案 → no_proposals（提炼没产出或已被消费）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        report = promotion_health(store, _config(), now=NOW)
        assert report["verdict"] == "no_proposals"
        assert report["pending"] == []
        assert report["last_promotion"] is None
        assert report["age_days"] is None


# ===== 3. 空转线 =====


def test_stale_flag_when_last_promotion_too_old():
    """距上次晋升超过 stale_days → stale=True（可疑空转，需人工查）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        old = (NOW - datetime.timedelta(days=20)).isoformat(timespec="seconds")
        store.append_promotion(
            action="applied",
            target="perspective",
            path="perspective.world_view",
            old_value="旧",
            new_value="新",
            reason="promote_minor",
            ts=old,
        )

        report = promotion_health(store, _config(), now=NOW)
        assert report["last_promotion"]["ts"] == old
        assert report["age_days"] == 20
        assert report["stale"] is True


def test_not_stale_when_recent():
    """距上次晋升很近 → stale=False。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        recent = (NOW - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        store.append_promotion(
            action="applied",
            target="perspective",
            path="perspective.world_view",
            old_value="旧",
            new_value="新",
            reason="promote_minor",
            ts=recent,
        )
        report = promotion_health(store, _config(), now=NOW)
        assert report["stale"] is False
        assert report["age_days"] == 1


# ===== 4. 只读性 =====


def test_health_does_not_write_anything():
    """诊断必须只读：跑完之后提案与 kv 数量都不变。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        refs = _seed_days(store, 4)
        _add_proposal(store, path="perspective.world_view", confidence=0.9, refs=refs)

        before_proposals = len(store.list_proposals(limit=500))
        before_promotions = len(store.list_promotions(limit=500))
        before_kv = store.get_kv_str("promotion:cooldown:perspective.world_view", "")

        promotion_health(store, _config(), now=NOW)

        assert len(store.list_proposals(limit=500)) == before_proposals
        assert len(store.list_promotions(limit=500)) == before_promotions
        assert (
            store.get_kv_str("promotion:cooldown:perspective.world_view", "") == before_kv
        )


# ===== 5. 证据池复用提炼端实现 =====


def test_evidence_pool_counts_only_eligible():
    """证据池只数白名单 kind（life / daily），promotion 留痕不计入。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = _make_store(tmp)
        _seed_days(store, 3)
        store.append_chronicle("self", "promotion", "看法更新（promote_minor）：x ← y")

        report = promotion_health(store, _config(), now=NOW)
        # 3 天 × 1 条 life；promotion 那条被白名单挡在池外
        assert report["evidence_pool"]["entries"] == 3
        assert report["evidence_pool"]["scenes"] == 3
        assert report["evidence_pool"]["days"] == 3


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
