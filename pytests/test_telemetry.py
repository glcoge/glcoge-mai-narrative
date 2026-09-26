"""Telemetry 采样判定单测（2026-09-13 体检 C5：判定逻辑自 plugin.py hook 下沉）。

口径与下沉前逐条对齐：
- 指标 1 user_initiated_freq：距上次出站 > 5 分钟才算用户主动发起；
- 指标 2 rounds：出站在入站 30 分钟窗口内到达配对 1 轮，user_id 随行；
- 指标 2 user_msg_len/bot_msg_len：按消息长度记录；
- 关键纪律：跟踪状态（_pending_round/_last_bot_sent）更新**无条件**，
  telemetry.enabled=false 时只是不写 CSV（A/B 对照窗口依赖此语义）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_telemetry.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_telemetry.py
"""

from __future__ import annotations

import datetime
import logging
import sys
from types import SimpleNamespace

import _synth_loader

_TELEMETRY_MOD = _synth_loader.load("services.state.snapshot")

Telemetry = _TELEMETRY_MOD.Telemetry

_LOGGER = logging.getLogger("telemetry-test")
_NOW = datetime.datetime(2026, 9, 13, 12, 0, 0)


class _FakeStore:
    """捕获 append_metric 调用的假 store。"""

    def __init__(self) -> None:
        self.metric_rows: list = []

    def append_metric(self, name, value, user_id="", scope="", ts=None) -> None:
        self.metric_rows.append(
            SimpleNamespace(name=name, value=value, user_id=user_id, scope=scope)
        )


def _make_telemetry(*, telemetry_enabled: bool = True) -> tuple:
    store = _FakeStore()
    cfg = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        telemetry=SimpleNamespace(enabled=telemetry_enabled),
    )
    telemetry = Telemetry(
        SimpleNamespace(config=cfg, _store=store, ctx=SimpleNamespace(logger=_LOGGER))
    )
    return telemetry, store


def _rows(store, scope: str) -> list:
    return [r for r in store.metric_rows if r.scope == scope]


def test_inbound_registers_round_and_user_initiated():
    """入站：登记待配对轮次；距上次出站 > 5 分钟记用户主动发起 + 入站长度。"""
    telemetry, store = _make_telemetry()

    telemetry.note_inbound(stream_id="s1", user_id="u1", text="你好呀", now=_NOW)

    assert "s1" in telemetry._pending_round, "入站应登记待配对轮次"
    initiated = [r for r in store.metric_rows if r.name == "user_initiated_freq"]
    assert len(initiated) == 1, "无出站历史（epoch 起算）应视为用户主动发起"
    assert initiated[0].user_id == "u1"
    lens = _rows(store, "user_msg_len")
    assert len(lens) == 1 and lens[0].value == 3.0


def test_inbound_after_recent_bot_reply_not_user_initiated():
    """bot 刚回复过（< 5 分钟）→ 入站不算用户主动发起。"""
    telemetry, store = _make_telemetry()
    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "我来说"}]},
        now=_NOW - datetime.timedelta(minutes=2),
    )
    store.metric_rows.clear()

    telemetry.note_inbound(stream_id="s1", user_id="u1", text="嗯", now=_NOW)

    assert not [r for r in store.metric_rows if r.name == "user_initiated_freq"], (
        "距 bot 出站仅 2 分钟，不应记用户主动发起"
    )


def test_outbound_pairs_round_within_window():
    """出站：窗口内的待配对入站 → 1 轮，user_id 随行，待办消费。"""
    telemetry, store = _make_telemetry()
    telemetry.note_inbound(
        stream_id="s1", user_id="u1", text="在吗",
        now=_NOW - datetime.timedelta(minutes=10),
    )

    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "我在"}]},
        now=_NOW,
    )

    rounds = _rows(store, "rounds")
    assert len(rounds) == 1 and rounds[0].value == 1 and rounds[0].user_id == "u1"
    assert "s1" not in telemetry._pending_round
    # bot 长度按 text 组件求和
    bot = _rows(store, "bot_msg_len")
    assert len(bot) == 1 and bot[0].value == 2.0


def test_outbound_expired_round_not_paired():
    """入站超 30 分钟窗口 → 不配对，但待办仍被消费。"""
    telemetry, store = _make_telemetry()
    telemetry._pending_round["s1"] = _NOW - datetime.timedelta(minutes=40)

    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "hi"}]},
        now=_NOW,
    )

    assert not _rows(store, "rounds")
    assert "s1" not in telemetry._pending_round


def test_state_updates_unconditional_when_disabled():
    """telemetry.enabled=false：不写 CSV，但轮次登记/出站时刻照常更新（纪律用例）。"""
    telemetry, store = _make_telemetry(telemetry_enabled=False)

    telemetry.note_inbound(stream_id="s1", user_id="u1", text="你好", now=_NOW)
    telemetry.note_outbound(
        stream_id="s1", user_id="u1",
        message={"raw_message": [{"type": "text", "data": "嗯"}]},
        now=_NOW + datetime.timedelta(minutes=1),
    )

    assert store.metric_rows == [], "采样关闭时不得写 CSV"
    assert "s1" not in telemetry._pending_round, "待办应照常被出站消费"
    assert "s1" in telemetry._last_bot_sent, "出站时刻应照常登记"


def test_outbound_without_stream_still_records_length():
    """无 stream_id 的出站：不更新轮次状态，但 bot 长度仍记录（与原 hook 一致）。"""
    telemetry, store = _make_telemetry()

    telemetry.note_outbound(
        stream_id="", user_id="",
        message={"raw_message": [{"type": "text", "data": "广播"}]},
        now=_NOW,
    )

    assert telemetry._last_bot_sent == {}
    bot = _rows(store, "bot_msg_len")
    assert len(bot) == 1 and bot[0].value == 2.0


# ===== 批 4-C10：晋升链路四计数器（R17 / P12） =====
#
# 批 1 建了 ``record_counter`` 通道，批 4 由晋升机真正写入。本节的**重点不是**
# 单元级「调用没调用」（那是各 C 的假计数器干的事），而是**端到端**「真的落到
# metrics/*.csv 了吗」——四指标的告警线要靠这些 csv 定（P12）。

from pathlib import Path  # noqa: E402

_STORE_MOD = _synth_loader.load("services.store")
_CONT_MOD = _synth_loader.load("services.state.continuity")
_ENGINE_MOD = _synth_loader.load("services.state.engine")
_PROPOSAL_MOD = _synth_loader.load("services.learning.proposal")

NarrativeStore = _STORE_MOD.NarrativeStore
PromotionEngine = _CONT_MOD.PromotionEngine
ProposalRunner = _PROPOSAL_MOD.ProposalRunner
default_self_state = _ENGINE_MOD.default_self_state
default_branch_state = _ENGINE_MOD.default_branch_state

COUNTER_KINDS = ("proposals", "promotions", "refutations", "rollbacks")


class _StackEngine:
    """晋升链路用的最小状态引擎假件。"""

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


class _StackClient:
    """假 LLM：返回给定文本。"""

    def __init__(self, response):
        self._response = response

    async def generate(self, prompt, *, temperature=None):
        return self._response


def _promotion_stack(tmp_path, *, telemetry_enabled: bool = True, **overrides):
    """真实 store + **真实 Telemetry** 的晋升链路（假件只留状态引擎与 LLM）。"""
    store = NarrativeStore(Path(tmp_path))
    engine = _StackEngine()
    plugin = SimpleNamespace(
        _store=store,
        _engine=engine,
        config=SimpleNamespace(
            plugin=SimpleNamespace(enabled=True),
            telemetry=SimpleNamespace(enabled=telemetry_enabled),
            promotion=_synth_loader.promotion_config(**overrides),
            llm=SimpleNamespace(
                creation_model="", temperature=0.9, creation_max_tokens=1024, show_prompt=False
            ),
            narrative=_synth_loader.sleep_config(sleep_time="", wake_time=""),
        ),
        ctx=SimpleNamespace(logger=_LOGGER),
    )
    plugin._telemetry = Telemetry(plugin)
    return plugin, store, engine


def _seed_and_apply(plugin, store, *, days: int = 3, value: str = "新看法"):
    for index in range(days):
        store.append_chronicle(
            "self", "life", f"素材 {index}", ts=f"2026-09-{10 + index:02d}T10:00:00"
        )
    refs = ",".join(f"chronicle:{row['id']}" for row in store.list_chronicle_rows("self", limit=50))
    store.add_proposal(
        target="perspective",
        path="perspective.world_view",
        proposed_value=value,
        confidence=0.9,
        status="pending",
        evidence_refs=refs,
    )
    proposal = store.list_proposals(status="pending", limit=1)[0]
    PromotionEngine(plugin).apply(proposal, now=_NOW)
    return proposal


def test_record_counter_accepts_only_registered_kinds():
    telemetry, store = _make_telemetry()
    for kind in COUNTER_KINDS:
        telemetry.record_counter(kind)
    assert sorted(r.name for r in _rows(store, "promotion")) == sorted(COUNTER_KINDS)

    try:
        telemetry.record_counter("typo_counter")
    except ValueError as exc:
        assert "未知的晋升计数器类型" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("未登记的 kind 必须被拒（写错了比不写更糟）")


def test_record_counter_respects_telemetry_gate():
    """⚠️ 已知耦合：关掉 telemetry = 关掉晋升链路的所有可观测性。"""
    telemetry, store = _make_telemetry(telemetry_enabled=False)
    telemetry.record_counter("promotions")
    assert store.metric_rows == []


def test_promotion_apply_lands_promotions_counter(tmp_path):
    """晋升成功 → ``metrics/promotions.csv`` 真的多一行（落盘，不只是内存计数）。"""
    plugin, store, _engine = _promotion_stack(tmp_path)
    _seed_and_apply(plugin, store)

    rows = store.read_metrics("promotions")
    assert len(rows) == 1
    assert rows[0]["scope"] == "promotion"
    assert (Path(tmp_path) / "metrics" / "promotions.csv").exists()


def test_proposal_run_lands_proposals_counter(tmp_path):
    """提案落库 → ``proposals`` 计数器落盘。"""
    import asyncio

    plugin, store, _engine = _promotion_stack(tmp_path)
    for index in range(6):
        store.append_chronicle(
            "self", "life", f"素材 {index}", ts=f"2026-09-{10 + index:02d}T10:00:00"
        )
    runner = ProposalRunner(plugin)
    runner._client = _StackClient(
        '[{"path": "perspective.world_view", "proposed_value": "她觉得世界很大",'
        ' "confidence": 0.8, "evidence_refs": ["1"], "cognitive_mode": "观察"}]'
    )
    result = asyncio.run(runner.run(now=_NOW))
    assert result["status"] == "ok"
    assert len(store.read_metrics("proposals")) == 1


def test_refutation_lands_refutations_counter(tmp_path):
    plugin, store, _engine = _promotion_stack(tmp_path, refutation_penalty=0.5)
    store.add_proposal(
        target="perspective",
        path="perspective.world_view",
        proposed_value="旧看法",
        confidence=0.6,
        status="pending",
        evidence_refs="chronicle:1",
    )
    store.add_proposal(
        target="perspective",
        path="perspective.world_view",
        proposed_value="反证",
        confidence=0.9,
        status="pending",
        evidence_refs="chronicle:2",
        contradicts="1",
    )
    assert PromotionEngine(plugin).apply_refutations(now=_NOW) == [1]
    assert len(store.read_metrics("refutations")) == 1


def test_rollback_lands_rollbacks_counter(tmp_path):
    plugin, store, _engine = _promotion_stack(tmp_path)
    _seed_and_apply(plugin, store)
    assert PromotionEngine(plugin).rollback_last(now=_NOW)["status"] == "rolled_back"
    assert len(store.read_metrics("rollbacks")) == 1


def test_all_four_counters_coexist_in_separate_csvs(tmp_path):
    """四指标各落各的 csv（同名混写会让告警线无法区分事件类型）。"""
    plugin, store, _engine = _promotion_stack(tmp_path, refutation_penalty=0.5)
    _seed_and_apply(plugin, store)
    engine = PromotionEngine(plugin)
    engine.rollback_last(now=_NOW)

    metrics_dir = Path(tmp_path) / "metrics"
    names = sorted(path.stem for path in metrics_dir.glob("*.csv"))
    assert "promotions" in names and "rollbacks" in names


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
