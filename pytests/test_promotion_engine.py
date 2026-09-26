"""晋升状态机（v0.2.0 批 4 · C4）：门槛 / 冷却 / 反证 / 场景计数 / 关系确定性晋升。

本文件是批 4 的**核心判据**，锁三条强约束与四条 HDSI 对策：

- **强约束 1（只吃正面信号）**：``promote_relationship`` 的输入只有正向场景日集合。
- **强约束 2（boundaries 冻结）**：任何输入下 ``relationship.boundaries`` 保持锚定值。
- **E13**：``stage`` 是派生结论（只读 trust+closeness），不独立晋升。
- **HDSI 3.1**：门槛四条（置信 / 场景 / 跨日 / 冷却）。
- **HDSI 3.3**：反证用 proposal id 引用，不做文本相似度；异路径引用不生效。
- **去重**（批 4 方案 §0.4 结论 2）：同一天多条素材只算 1 个场景。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_promotion_engine.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_promotion_engine.py
"""

from __future__ import annotations

import datetime
import tempfile
import types
from pathlib import Path

import _synth_loader

_CONT = _synth_loader.load("services.state.continuity")
_ENGINE = _synth_loader.load("services.state.engine")
_STORE = _synth_loader.load("services.store")

PromotionEngine = _CONT.PromotionEngine
RELATIONSHIP_AUTO_PROMOTE = _CONT.RELATIONSHIP_AUTO_PROMOTE
scene_stats = _CONT.scene_stats
meets_minor_gate = _CONT.meets_minor_gate
meets_major_gate = _CONT.meets_major_gate
is_after_cooldown = _CONT.is_after_cooldown
apply_refutation_penalty = _CONT.apply_refutation_penalty
merge_confidence = _CONT.merge_confidence
relation_value = _CONT.relation_value
derive_stage = _CONT.derive_stage
parse_id_list = _CONT.parse_id_list
parse_chronicle_refs = _CONT.parse_chronicle_refs
default_self_state = _ENGINE.default_self_state
default_branch_state = _ENGINE.default_branch_state
NarrativeStore = _STORE.NarrativeStore

NOW = datetime.datetime(2026, 9, 26, 12, 0, 0)


class _FakeEngine:
    """最小引擎假件：只管 self / branch state 的读写。"""

    def __init__(self):
        self.self_state = default_self_state()
        self.branches: dict = {}

    def load_self_state(self):
        return self.self_state

    def save_self_state(self, state):
        self.self_state = state

    def load_branch_state(self, uid):
        return self.branches.setdefault(str(uid), default_branch_state())

    def save_branch_state(self, uid, state):
        self.branches[str(uid)] = state


class _Counter:
    def __init__(self):
        self.kinds: list = []

    def record_counter(self, kind: str, value: float = 1) -> None:
        self.kinds.append(kind)


def _make(tmp: str, **overrides):
    store = NarrativeStore(Path(tmp))
    engine = _FakeEngine()
    counter = _Counter()
    plugin = types.SimpleNamespace(
        _store=store,
        _engine=engine,
        _telemetry=counter,
        config=types.SimpleNamespace(
            promotion=_synth_loader.promotion_config(**overrides)
        ),
        ctx=types.SimpleNamespace(logger=_synth_loader.null_logger()),
    )
    return PromotionEngine(plugin), store, engine, counter


def _seed_days(store, days: int, *, per_day: int = 1) -> str:
    """造 ``days`` 个自然日、每天 ``per_day`` 条素材；返回 ``chronicle:<id>`` 引用串。"""
    for index in range(days):
        for slot in range(per_day):
            store.append_chronicle(
                "self",
                "life",
                f"素材 {index}-{slot}",
                ts=f"2026-09-{10 + index:02d}T0{slot}:00:00",
            )
    rows = store.list_chronicle_rows("self", limit=500)
    return ",".join(f"chronicle:{row['id']}" for row in rows)


def _add(store, *, refs: str, confidence=0.9, path="perspective.world_view", contradicts=""):
    return store.add_proposal(
        target="perspective",
        path=path,
        proposed_value="她觉得世界比想象的大",
        confidence=confidence,
        status="pending",
        evidence_refs=refs,
        contradicts=contradicts,
    )


# ─── 纯函数 ─────────────────────────────────────────────────────


def test_scene_stats_dedupes_same_day():
    """同一天多条 → 1 场景 1 日（真实数据去重比最高 18.56）。"""
    rows = [
        {"ts": "2026-09-20T09:00:00", "source_uid": ""},
        {"ts": "2026-09-20T21:00:00", "source_uid": ""},
    ]
    assert scene_stats(rows) == (1, 1)


def test_scene_stats_separates_sources_same_day():
    """同一天不同来源 = 2 个场景，但仍算 1 个自然日。"""
    rows = [
        {"ts": "2026-09-20T09:00:00", "source_uid": ""},
        {"ts": "2026-09-20T10:00:00", "source_uid": "927386371"},
    ]
    assert scene_stats(rows) == (2, 1)


def test_scene_stats_counts_days():
    rows = [
        {"ts": "2026-09-20T09:00:00", "source_uid": ""},
        {"ts": "2026-09-21T09:00:00", "source_uid": ""},
        {"ts": "坏时间", "source_uid": ""},
    ]
    assert scene_stats(rows) == (2, 2)


def test_minor_gate_requires_all_three():
    ok = dict(min_confidence=0.82, min_scenes=3, min_days=2)
    assert meets_minor_gate(confidence=0.9, scene_count=3, day_count=2, **ok)
    assert not meets_minor_gate(confidence=0.81, scene_count=3, day_count=2, **ok)
    assert not meets_minor_gate(confidence=0.9, scene_count=2, day_count=2, **ok)
    assert not meets_minor_gate(confidence=0.9, scene_count=3, day_count=1, **ok)


def test_major_gate_ignores_days():
    assert meets_major_gate(confidence=0.96, scene_count=2, min_confidence=0.95, min_scenes=2)
    assert not meets_major_gate(
        confidence=0.96, scene_count=1, min_confidence=0.95, min_scenes=2
    )


def test_is_after_cooldown():
    assert is_after_cooldown("", now=NOW, cooldown_hours=72)
    assert is_after_cooldown("不是时间", now=NOW, cooldown_hours=72)
    assert not is_after_cooldown("2026-09-26T11:00:00", now=NOW, cooldown_hours=72)
    assert is_after_cooldown("2026-09-20T11:00:00", now=NOW, cooldown_hours=72)


def test_apply_refutation_penalty_floors_at_zero():
    assert apply_refutation_penalty(0.9, penalty=0.2) == 0.7
    assert apply_refutation_penalty(0.1, penalty=0.2) == 0.0


def test_merge_confidence_only_bonuses_new_scene():
    """复述不加信（HDSI 2.1）：非新场景时原值不动。"""
    assert merge_confidence(0.9, 0.5, bonus=0.05, new_scene=True) == 0.55
    assert merge_confidence(0.9, 0.5, bonus=0.05, new_scene=False) == 0.9
    # 取 min：合并不会把提案推高到超过本次自评
    assert merge_confidence(0.52, 0.5, bonus=0.05, new_scene=True) == 0.52


def test_relation_value_curve_e14():
    """E14 曲线：达到起步门槛即得第一档，之后每档 +step，封顶 max。"""
    curve = dict(min_days=3, days_per_step=2, step=0.05, max_value=0.8)
    assert relation_value(0, **curve) == 0.0
    assert relation_value(2, **curve) == 0.0
    assert relation_value(3, **curve) == 0.05
    assert relation_value(4, **curve) == 0.05
    assert relation_value(5, **curve) == 0.10
    assert relation_value(19, **curve) == 0.45
    assert relation_value(33, **curve) == 0.80
    assert relation_value(999, **curve) == 0.80  # 封顶


def test_derive_stage_bands():
    assert derive_stage(0.0, 0.0) == "陌生人"
    assert derive_stage(0.3, 0.3) == "相识"
    assert derive_stage(0.5, 0.5) == "亲近"
    assert derive_stage(0.8, 0.8) == "很亲近"


def test_parse_helpers():
    assert parse_id_list("3, 5 ,x,7") == [3, 5, 7]
    assert parse_chronicle_refs("chronicle:11, chronicle:12") == [11, 12]
    assert parse_chronicle_refs("11,12") == [11, 12]
    assert parse_chronicle_refs("") == []


def test_auto_promote_excludes_boundaries():
    """**强约束 2 的常量断言**：自动晋升目标里不得出现 boundaries。"""
    assert RELATIONSHIP_AUTO_PROMOTE == ("trust", "closeness")
    assert "boundaries" not in RELATIONSHIP_AUTO_PROMOTE
    assert "stage" not in RELATIONSHIP_AUTO_PROMOTE  # stage 是派生，不独立晋升


# ─── evaluate ───────────────────────────────────────────────────


def test_evaluate_below_gate():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp)
        refs = _seed_days(store, 2)
        engine_ = engine
        proposal = {"id": _add(store, refs=refs), "path": "perspective.world_view",
                    "confidence": 0.9, "evidence_refs": refs, "target": "perspective"}
        decision = engine_.evaluate(proposal, now=NOW)
        assert decision["promote"] is False
        assert decision["reason"] == "below_gate"
        assert (decision["scene_count"], decision["day_count"]) == (2, 2)


def test_evaluate_promotes_on_gate():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp)
        refs = _seed_days(store, 3)
        proposal = {"id": _add(store, refs=refs), "path": "perspective.world_view",
                    "confidence": 0.9, "evidence_refs": refs, "target": "perspective"}
        decision = engine.evaluate(proposal, now=NOW)
        assert decision["promote"] is True
        assert decision["reason"] == "promote_minor"


def test_evaluate_same_day_many_entries_still_below_gate():
    """同一天 10 条素材 = 1 场景 1 日 → 仍不够门槛（防单日刷屏，§0.4 结论 2）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp)
        refs = _seed_days(store, 1, per_day=10)
        proposal = {"id": _add(store, refs=refs), "path": "perspective.world_view",
                    "confidence": 0.99, "evidence_refs": refs, "target": "perspective"}
        decision = engine.evaluate(proposal, now=NOW)
        assert decision["promote"] is False
        assert (decision["scene_count"], decision["day_count"]) == (1, 1)


def test_evaluate_respects_cooldown():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp)
        refs = _seed_days(store, 3)
        engine.mark_cooldown("perspective.world_view", NOW - datetime.timedelta(hours=1))
        proposal = {"id": _add(store, refs=refs), "path": "perspective.world_view",
                    "confidence": 0.9, "evidence_refs": refs, "target": "perspective"}
        assert engine.evaluate(proposal, now=NOW)["reason"] == "cooldown"


def test_evaluate_major_disabled_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp)
        refs = _seed_days(store, 2)
        proposal = {"id": _add(store, refs=refs, confidence=0.99),
                    "path": "perspective.world_view", "confidence": 0.99,
                    "evidence_refs": refs, "target": "perspective"}
        assert engine.evaluate(proposal, now=NOW)["promote"] is False


def test_evaluate_major_when_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp, major_enabled=True)
        refs = _seed_days(store, 2)
        proposal = {"id": _add(store, refs=refs, confidence=0.99),
                    "path": "perspective.world_view", "confidence": 0.99,
                    "evidence_refs": refs, "target": "perspective"}
        decision = engine.evaluate(proposal, now=NOW)
        assert decision["promote"] is True
        assert decision["reason"] == "promote_major"


# ─── apply ──────────────────────────────────────────────────────


def test_apply_writes_state_and_audit():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, counter = _make(tmp)
        refs = _seed_days(store, 3)
        proposal_id = _add(store, refs=refs, confidence=0.9)

        result = engine.apply(
            {"id": proposal_id, "path": "perspective.world_view", "confidence": 0.9,
             "evidence_refs": refs, "target": "perspective",
             "proposed_value": "她觉得世界比想象的大"},
            now=NOW,
        )
        assert result["applied"] is True
        # 现值写进 self state
        assert fake_engine.self_state["perspective"]["world_view"] == "她觉得世界比想象的大"
        assert fake_engine.self_state["perspective"]["origin"] == "promotion"
        # 提案状态 + 审计 + 留痕
        assert store.list_proposals(status="applied")[0]["id"] == proposal_id
        promotions = store.list_promotions(path="perspective.world_view")
        assert promotions[0]["action"] == "applied"
        assert promotions[0]["new_value"] == "她觉得世界比想象的大"
        # 人可读留痕（kind=promotion，该 kind 本身被证据白名单拒绝，不会自证循环）
        kinds = [row["kind"] for row in store.list_chronicle_rows("self", limit=10)]
        assert "promotion" in kinds
        # 冷却已标记 + 计数器
        assert engine.is_cooling("perspective.world_view", NOW)
        assert counter.kinds == ["promotions"]


def test_apply_below_gate_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, counter = _make(tmp)
        refs = _seed_days(store, 1)
        proposal_id = _add(store, refs=refs, confidence=0.9)

        result = engine.apply(
            {"id": proposal_id, "path": "perspective.world_view", "confidence": 0.9,
             "evidence_refs": refs, "target": "perspective", "proposed_value": "x"},
            now=NOW,
        )
        assert result["applied"] is False
        assert fake_engine.self_state["perspective"]["world_view"] == ""
        assert store.list_proposals(status="pending")[0]["id"] == proposal_id
        assert store.list_promotions() == []
        assert counter.kinds == []


# ─── 反证 ───────────────────────────────────────────────────────


def test_refutation_rejects_target():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, counter = _make(tmp)
        target_id = _add(store, refs="chronicle:1", confidence=0.85)
        _add(store, refs="chronicle:2", confidence=0.9, contradicts=str(target_id))

        rejected = engine.apply_refutations(now=NOW)
        assert rejected == [target_id]
        assert store.list_proposals(status="rejected")[0]["id"] == target_id
        assert counter.kinds == ["refutations"]
        audit = store.list_promotions(path="perspective.world_view")
        assert audit[0]["action"] == "rejected"
        assert audit[0]["reason"] == f"refuted_by:{target_id + 1}"


def test_refutation_ignores_different_path():
    """契约校验：反证必须指向**同路径**的提案（不做文本相似度）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, counter = _make(tmp)
        target_id = _add(store, refs="chronicle:1", confidence=0.85)
        # 引用方是另一条路径（life_goals）
        _add(
            store,
            refs="chronicle:2",
            confidence=0.9,
            path="perspective.life_goals",
            contradicts=str(target_id),
        )
        assert engine.apply_refutations(now=NOW) == []
        # 两条都还在 pending（异路径反证不生效；list_proposals 是 id DESC，按 id 定位）
        pending = {row["id"]: row for row in store.list_proposals(status="pending")}
        assert set(pending) == {target_id, target_id + 1}
        assert pending[target_id]["confidence"] == 0.85  # 信度未被扣
        assert counter.kinds == []


def test_refutation_keeps_pending_when_still_above_threshold():
    """扣信后仍 ≥ 门槛 → 保持 pending（只降信度，不驳回）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp, refutation_penalty=0.05)
        target_id = _add(store, refs="chronicle:1", confidence=0.99)
        _add(store, refs="chronicle:2", confidence=0.9, contradicts=str(target_id))

        assert engine.apply_refutations(now=NOW) == []
        pending = {row["id"]: row for row in store.list_proposals(status="pending")}
        assert pending[target_id]["status"] == "pending"
        assert pending[target_id]["confidence"] == 0.94  # 0.99 - 0.05，仍 ≥ 0.82
        assert store.list_promotions(path="perspective.world_view") == []


# ─── 关系确定性晋升（强约束 1/2 + E13/E14） ─────────────────────


def test_promote_relationship_advances_trust_and_closeness():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, counter = _make(tmp)
        days = {f"2026-09-{10 + index:02d}" for index in range(5)}

        result = engine.promote_relationship("u1", days, now=NOW)
        assert result["changed"] == {"trust": 0.10, "closeness": 0.10}
        relationship = fake_engine.branches["u1"]["relationship"]
        assert relationship["trust"] == 0.10
        assert relationship["closeness"] == 0.10
        assert counter.kinds == ["promotions", "promotions"]
        # 审计分别记两条
        assert len(store.list_promotions(path="relationship.trust")) == 1
        assert len(store.list_promotions(path="relationship.closeness")) == 1


def test_promote_relationship_freezes_boundaries():
    """**强约束 2 的核心断言**：任何正向信号都不许推动 boundaries。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        days = {f"2026-09-{10 + index:02d}" for index in range(19)}  # 高分场景

        engine.promote_relationship("u1", days, now=NOW)
        relationship = fake_engine.branches["u1"]["relationship"]
        assert relationship["boundaries"] == 0.0
        assert relationship["trust"] > 0.0
        assert store.list_promotions(path="relationship.boundaries") == []


def test_promote_relationship_no_change_below_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, counter = _make(tmp)
        result = engine.promote_relationship("u1", {"2026-09-10", "2026-09-11"}, now=NOW)
        assert result["value"] == 0.0
        assert result["changed"] == {}
        assert counter.kinds == []


def test_promote_relationship_updates_stage():
    """E13：stage 随 trust+closeness 派生（不独立晋升）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        # 19 日 → 0.45 → 均值 0.45 → 「相识」
        days = {f"2026-09-{10 + index:02d}" for index in range(19)}
        result = engine.promote_relationship("u1", days, now=NOW)
        assert result["stage"] == "相识"
        assert fake_engine.branches["u1"]["relationship"]["stage"] == "相识"


def test_promote_relationship_respects_max_cap():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp, relation_max=0.2)
        days = {f"2026-09-{10 + index:02d}" for index in range(19)}
        engine.promote_relationship("u1", days, now=NOW)
        assert fake_engine.branches["u1"]["relationship"]["trust"] == 0.2


def test_promote_relationship_is_monotonic():
    """值只会前进：已有更高值时不被更低的计数拉回去（只进不退的**证据**语义）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        fake_engine.branches["u1"] = default_branch_state()
        fake_engine.branches["u1"]["relationship"]["trust"] = 0.5

        result = engine.promote_relationship(
            "u1", {f"2026-09-{10 + index:02d}" for index in range(5)}, now=NOW
        )
        assert "trust" not in result["changed"]
        assert fake_engine.branches["u1"]["relationship"]["trust"] == 0.5


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
