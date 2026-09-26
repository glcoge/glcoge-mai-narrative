"""慢变晋升回滚（v0.2.0 批 4 · C8）：按审计表逆序恢复旧值 + 留痕 + 命令门禁。

慢变区的纪律是「小步可动，**须留痕**」——留痕的意义就在这里：能撤。
本单元锁四件事：

- **回滚依据是审计表的 old_value**，不是「把值清零」：必须精确恢复成晋升前那个值。
- **只回滚一步**：一次撤多步会让「哪一步被撤了」无法叙述（留痕链断裂）。
- **per_user 拒绝瞎猜**：关系维度按 uid 隔离，审计里没有 ``source_uid`` 就拒绝执行。
- **类型还原**：审计值恒为字符串，但 ``life_goals`` 是列表、``trust`` 是浮点——
  直接写回是静默数据损坏（本批实现期发现，见 ``coerce_slow_value``）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_promotion_rollback.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_promotion_rollback.py
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
_synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

PromotionEngine = _CONT.PromotionEngine
coerce_slow_value = _CONT.coerce_slow_value
audit_text = _CONT.audit_text
default_self_state = _ENGINE.default_self_state
default_branch_state = _ENGINE.default_branch_state
NarrativeStore = _STORE.NarrativeStore
MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin

NOW = datetime.datetime(2026, 9, 26, 12, 0, 0)
LATER = NOW + datetime.timedelta(hours=100)


class _FakeEngine:
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
        config=types.SimpleNamespace(promotion=_synth_loader.promotion_config(**overrides)),
        ctx=types.SimpleNamespace(logger=_synth_loader.null_logger()),
    )
    return PromotionEngine(plugin), store, engine, counter


def _seed_days(store, days: int, *, prefix="2026-09") -> str:
    for index in range(days):
        store.append_chronicle(
            "self", "life", f"素材 {index}", ts=f"{prefix}-{10 + index:02d}T10:00:00"
        )
    rows = store.list_chronicle_rows("self", limit=500)
    return ",".join(f"chronicle:{row['id']}" for row in rows)


def _add(store, *, refs, value="她相信世界比想象的大", confidence=0.9,
         path="perspective.world_view"):
    return store.add_proposal(
        target="perspective",
        path=path,
        proposed_value=value,
        confidence=confidence,
        status="pending",
        evidence_refs=refs,
    )


def _apply(engine, store, *, path="perspective.world_view", value="新看法", days=3, now=NOW):
    refs = _seed_days(store, days, prefix="2026-09" if days <= 15 else "2026-10")
    proposal_id = _add(store, refs=refs, value=value, path=path)
    row = store.list_proposals(status="pending", limit=1)[0]
    engine.apply(row, now=now)
    return proposal_id


# ─── 类型还原（实现期发现的静默损坏） ───────────────────────────


def test_coerce_list_field():
    assert coerce_slow_value("perspective.life_goals", '["学会游泳", "读完那本书"]') == [
        "学会游泳",
        "读完那本书",
    ]
    assert coerce_slow_value("perspective.life_goals", "学会游泳") == ["学会游泳"]
    assert coerce_slow_value("perspective.life_goals", "学会游泳；读完那本书") == [
        "学会游泳",
        "读完那本书",
    ]
    assert coerce_slow_value("perspective.life_goals", ["A"]) == ["A"]
    assert coerce_slow_value("perspective.life_goals", "") == []


def test_coerce_float_field():
    assert coerce_slow_value("relationship.trust", "0.35") == 0.35
    assert coerce_slow_value("relationship.trust", "不是数") == 0.0


def test_coerce_text_field_and_stage_is_not_float():
    assert coerce_slow_value("perspective.world_view", 123) == "123"
    # stage 是阶段标签，不是数值——不得被当成浮点还原
    assert coerce_slow_value("relationship.stage", "亲近") == "亲近"


def test_audit_text_roundtrip_for_lists():
    """列表落审计必须可逆（str() 会给出单引号的 '['a']'，解析不回来）。"""
    payload = ["学会游泳"]
    assert coerce_slow_value("perspective.life_goals", audit_text(payload)) == payload


def test_apply_restores_list_type_for_life_goals():
    """**回归**：提案值恒为字符串，但写进 life_goals 必须是列表。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        _apply(engine, store, path="perspective.life_goals", value="学会游泳")
        assert fake_engine.self_state["perspective"]["life_goals"] == ["学会游泳"]


# ─── 回滚：perspective ─────────────────────────────────────────


def test_rollback_restores_previous_value():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, counter = _make(tmp)
        proposal_id = _apply(engine, store, value="她觉得世界比想象的大")
        assert fake_engine.self_state["perspective"]["world_view"] == "她觉得世界比想象的大"

        result = engine.rollback_last(now=LATER)
        assert result["status"] == "rolled_back"
        assert result["path"] == "perspective.world_view"
        assert result["restored"] == ""  # 晋升前的旧值就是空
        assert fake_engine.self_state["perspective"]["world_view"] == ""


def test_rollback_writes_audit_and_marks_proposal():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, counter = _make(tmp)
        proposal_id = _apply(engine, store)
        engine.rollback_last(now=LATER)

        audit = store.list_promotions(path="perspective.world_view")
        assert audit[0]["action"] == "rolled_back"
        assert audit[0]["new_value"] == ""  # 恢复出来的值
        assert audit[0]["old_value"] == "新看法"  # 被撤掉的值
        assert audit[0]["reason"].startswith("rollback_of:")
        assert store.list_proposals(status="rolled_back")[0]["id"] == proposal_id
        assert counter.kinds.count("rollbacks") == 1


def test_rollback_writes_human_readable_chronicle():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, _c = _make(tmp)
        _apply(engine, store)
        engine.rollback_last(now=LATER)
        texts = [row["text"] for row in store.list_chronicle_rows("self", limit=20)]
        assert any("回滚" in text for text in texts)


def test_rollback_nothing_when_no_applied():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, _e, counter = _make(tmp)
        result = engine.rollback_last(now=NOW)
        assert result["status"] == "nothing"
        assert store.list_promotions() == []
        assert counter.kinds == []


def test_rollback_only_one_step():
    """**只回滚一步**：两次晋升 → 一次回滚只退到上一步，不是清零。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        _apply(engine, store, value="第一版看法", days=3)
        _apply(engine, store, value="第二版看法", days=6, now=LATER)
        assert fake_engine.self_state["perspective"]["world_view"] == "第二版看法"

        assert engine.rollback_last(now=LATER)["restored"] == "第一版看法"
        assert fake_engine.self_state["perspective"]["world_view"] == "第一版看法"

        assert engine.rollback_last(now=LATER)["restored"] == ""
        assert fake_engine.self_state["perspective"]["world_view"] == ""


def test_rollback_respects_path_filter():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        _apply(engine, store, path="perspective.world_view", value="看法")
        _apply(engine, store, path="perspective.life_goals", value="学会游泳", days=6, now=LATER)

        result = engine.rollback_last(path="perspective.world_view", now=LATER)
        assert result["path"] == "perspective.world_view"
        assert fake_engine.self_state["perspective"]["world_view"] == ""
        # life_goals 未被波及
        assert fake_engine.self_state["perspective"]["life_goals"] == ["学会游泳"]


def test_rollback_marks_cooldown():
    """撤完立刻再晋升同值没有意义（同一批证据会立刻推回去）→ 冷却照标。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        _apply(engine, store)
        engine.rollback_last(now=LATER)
        assert engine.is_cooling("perspective.world_view", LATER)


# ─── 回滚：relationship（per_user 严格隔离） ────────────────────


def test_rollback_relationship_uses_source_uid():
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp, relation_max=0.2)
        days = {f"2026-09-{10 + index:02d}" for index in range(19)}
        engine.promote_relationship("927386371", days, now=NOW)
        assert fake_engine.branches["927386371"]["relationship"]["trust"] == 0.2

        result = engine.rollback_last(path="relationship.trust", now=LATER)
        assert result["status"] == "rolled_back"
        assert result["scope"] == "927386371"
        assert fake_engine.branches["927386371"]["relationship"]["trust"] == 0.0


def test_rollback_relationship_rejects_without_scope():
    """审计里没有 source_uid → **拒绝执行**（宁可不动，也不猜是谁）。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine, store, fake_engine, _c = _make(tmp)
        store.append_promotion(
            action="applied",
            target="relationship",
            path="relationship.trust",
            old_value="0.0",
            new_value="0.2",
            ts=NOW.isoformat(timespec="seconds"),
        )
        result = engine.rollback_last(path="relationship.trust", now=LATER)
        assert result["status"] == "missing_scope"
        assert fake_engine.branches == {}, "缺 scope 时不得写任何支线"


# ─── 命令门禁 ───────────────────────────────────────────────────


class _Sender:
    def __init__(self):
        self.messages: list = []

    async def text(self, message, stream_id):
        self.messages.append((message, stream_id))


def _command_plugin(tmp_store, engine, *, rollback_result=None):
    """给命令层造一个最小 fake：只喂 handle_narrative_command 用到的属性。"""
    sender = _Sender()
    fake = types.SimpleNamespace(
        _store=tmp_store,
        _engine=None,  # 让 _rebuild_learned 走早退分支，不必真写 config.toml
        _promotion_engine=types.SimpleNamespace(
            rollback_last=lambda *, path="", now: (rollback_result or {"status": "nothing"})
        ),
        _local_now=lambda: NOW,
        ctx=types.SimpleNamespace(send=sender, logger=_synth_loader.null_logger()),
        config=types.SimpleNamespace(plugin=types.SimpleNamespace(admin_qq=["1"])),
    )
    fake._rebuild_learned = lambda: MaiNarrativePlugin._rebuild_learned(fake)
    fake._is_admin = lambda user_id: user_id in {"1"}
    fake._cmd_rollback = lambda param, stream_id: MaiNarrativePlugin._cmd_rollback(
        fake, param, stream_id
    )
    return fake, sender


def _run_command(fake, sub: str):
    import asyncio

    return asyncio.run(
        MaiNarrativePlugin.handle_narrative_command(
            fake, matched_groups={"sub": sub}, stream_id="s1", user_id="1"
        )
    )


def test_command_rejects_non_admin():
    with tempfile.TemporaryDirectory() as tmp:
        store = NarrativeStore(Path(tmp))
        fake, sender = _command_plugin(store, None)
        import asyncio

        asyncio.run(
            MaiNarrativePlugin.handle_narrative_command(
                fake, matched_groups={"sub": "rollback last"}, stream_id="s1", user_id="999"
            )
        )
        assert "仅管理员" in sender.messages[0][0]


def test_command_rejects_non_whitelisted_path():
    """路径必须落在慢变白名单（不然等于开了个任意写入后门）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = NarrativeStore(Path(tmp))
        fake, sender = _command_plugin(store, None)
        _run_command(fake, "rollback perspective.favorite_food")
        assert "不在慢变白名单" in sender.messages[0][0]


def test_command_reports_nothing_to_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        store = NarrativeStore(Path(tmp))
        fake, sender = _command_plugin(store, None)
        _run_command(fake, "rollback last")
        assert "没有可撤销的晋升记录" in sender.messages[0][0]


def test_command_reports_success_with_path_and_value():
    with tempfile.TemporaryDirectory() as tmp:
        store = NarrativeStore(Path(tmp))
        result = {
            "status": "rolled_back",
            "path": "perspective.world_view",
            "restored": "",
            "promotion_id": 7,
        }
        fake, sender = _command_plugin(store, None, rollback_result=result)
        _run_command(fake, "rollback last")
        message = sender.messages[0][0]
        assert "已撤销" in message
        assert "perspective.world_view" in message


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
