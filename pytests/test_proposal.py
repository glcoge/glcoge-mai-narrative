"""慢变提案通道（v0.2.0 批 4 · C3）：契约校验 / 空提案 / 退避 / 输入充分性。

锁定四条 HDSI 事故的对策（批 4 方案 §5）：

- **2.1 期待→被爽约**：证据必须可溯源（引用编号→池内校验，不信任模型给的 id）；
  ``条件``/``提议`` 认知模式**不降级**为最宽松的「观察」（降级＝放行）。
- **1.10 温水杯**：``pending`` 回传**只含 id/status/path/value**，``evidence`` 不回传。
- **5.3 失败重试风暴**：空提案也写 ``last_ts``；失败退避**只在内存** → 重启必再试一次。
- **5.9 Agency 恒空**：证据不足**不调 LLM**，且**不写 last_ts**（证据够了要立刻有机会）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_proposal.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_proposal.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import tempfile
import types
from pathlib import Path

import _synth_loader

_PROPOSAL = _synth_loader.load("services.learning.proposal")
_STORE = _synth_loader.load("services.store")

ProposalRunner = _PROPOSAL.ProposalRunner
build_proposal_prompt = _PROPOSAL.build_proposal_prompt
parse_proposals = _PROPOSAL.parse_proposals
collect_evidence = _PROPOSAL.collect_evidence
pending_digest = _PROPOSAL.pending_digest
NEGATIVE_CHECKLIST = _PROPOSAL.NEGATIVE_CHECKLIST
NarrativeStore = _STORE.NarrativeStore

NOW = datetime.datetime(2026, 9, 26, 12, 0, 0)


class _FakeClient:
    """假 LLM 客户端（记录 prompt，可注入失败）。"""

    def __init__(self, response: str = "", *, fail: bool = False):
        self._response = response
        self._fail = fail
        self.calls: list = []

    async def generate(self, prompt: str, *, temperature=None) -> str:
        self.calls.append({"prompt": prompt, "temperature": temperature})
        if self._fail:
            return ""
        return self._response


class _Counter:
    def __init__(self):
        self.kinds: list = []

    def record_counter(self, kind: str, value: float = 1) -> None:
        self.kinds.append(kind)


def _make(tmp: str, *, response="", fail=False, counters=None, **config_overrides):
    """构造 (runner, store, counters)：store 用**真实** sqlite（临时目录）。"""
    store = NarrativeStore(Path(tmp))
    counter = counters if counters is not None else _Counter()
    plugin = types.SimpleNamespace(
        _store=store,
        _telemetry=counter,
        config=types.SimpleNamespace(
            promotion=_synth_loader.promotion_config(**config_overrides),
            llm=types.SimpleNamespace(
                creation_model="", temperature=0.9, creation_max_tokens=1024, show_prompt=False
            ),
            plugin=types.SimpleNamespace(enabled=True),
        ),
        ctx=types.SimpleNamespace(logger=_synth_loader.null_logger()),
    )
    runner = ProposalRunner(plugin)
    runner._client = _FakeClient(response, fail=fail)
    return runner, store, counter


def _seed_evidence(store, count: int = 6, *, kind: str = "life", source_uid: str = "") -> None:
    for index in range(count):
        store.append_chronicle(
            "self", kind, f"第 {index} 条素材", ts=f"2026-09-{10 + index:02d}T10:00:00",
            source_uid=source_uid,
        )


def _proposal_json(**overrides) -> str:
    """默认用**池外模式**的 refs 形状（``chronicle:<id>``）。

    带编号（``["1"]``）的 refs 只在传了 ``allowed_refs`` 的池模式下合法——
    这是刻意的：模型给编号、池负责映射成原文 id，二者不可混用。
    """
    item = {
        "target": "perspective",
        "path": "perspective.world_view",
        "proposed_value": "她觉得世界比想象的大",
        "confidence": 0.86,
        "evidence_refs": ["chronicle:11", "chronicle:12"],
        "cognitive_mode": "观察",
        "holder": "她",
        "contradicts": [],
    }
    item.update(overrides)
    return json.dumps([item], ensure_ascii=False)


def _pool_proposal_json(**overrides) -> str:
    """**池模式**的模型输出：``evidence_refs`` 给的是**证据编号**（池负责映射）。

    ``run()`` 走的就是池模式（编号 → ``chronicle:<id>`` 由代码决定，不信任模型
    自己拼 id）。
    """
    return _proposal_json(evidence_refs=["1", "2"], **overrides)


# ─── prompt 组装 ────────────────────────────────────────────────


def test_prompt_contains_negative_checklist():
    """HDS 负面清单必须在 prompt 里（防有人「精简提示词」时把它删掉）。"""
    prompt = build_proposal_prompt([{"id": 1, "text": "素材", "kind": "life"}])
    assert "不是" in prompt and "发展倾向" in prompt
    assert "口吻" in NEGATIVE_CHECKLIST and "暂时性情绪" in NEGATIVE_CHECKLIST


def test_prompt_numbers_evidence():
    entries = [{"id": 7, "text": "在看书"}, {"id": 9, "text": "想学游泳"}]
    prompt = build_proposal_prompt(entries)
    assert "[1] 在看书" in prompt
    assert "[2] 想学游泳" in prompt


def test_prompt_handles_empty_evidence():
    prompt = build_proposal_prompt([])
    assert "（无）" in prompt


def test_prompt_pending_has_no_evidence_field():
    """pending 摘要进 prompt 时**不得**夹带 evidence（HDSI 1.10 防自我强化）。"""
    prompt = build_proposal_prompt(
        [{"id": 1, "text": "x"}],
        pending=[{"id": 3, "status": "pending", "path": "perspective.world_view", "value": "旧看法"}],
    )
    assert "id=3" in prompt
    assert "旧看法" in prompt
    assert "evidence" not in prompt.split("【输出】")[0].replace("evidence_refs", "")


# ─── 契约解析 ───────────────────────────────────────────────────


def test_parse_valid_proposal():
    result = parse_proposals(_proposal_json())
    assert result["discarded"] == {}
    assert len(result["proposals"]) == 1
    proposal = result["proposals"][0]
    assert proposal["path"] == "perspective.world_view"
    assert proposal["confidence"] == 0.86
    assert proposal["cognitive_mode"] == "观察"


def test_parse_rejects_non_whitelisted_path():
    result = parse_proposals(_proposal_json(path="perspective.favorite_food"))
    assert result["proposals"] == []
    assert result["discarded"]["path_not_allowed"] == 1


def test_parse_rejects_relationship_path():
    """**只服务 perspective**：relationship 走确定性通道，不进 LLM 提案。"""
    result = parse_proposals(_proposal_json(path="relationship.closeness"))
    assert result["proposals"] == []
    assert result["discarded"]["path_not_allowed"] == 1


def test_parse_clamps_confidence():
    assert parse_proposals(_proposal_json(confidence=9.9))["proposals"][0]["confidence"] == 1.0
    assert parse_proposals(_proposal_json(confidence=-3))["proposals"][0]["confidence"] == 0.0
    assert parse_proposals(_proposal_json(confidence="不是数"))["proposals"][0]["confidence"] == 0.0


def test_parse_rejects_empty_value():
    result = parse_proposals(_proposal_json(proposed_value="   "))
    assert result["discarded"]["empty_value"] == 1


def test_parse_rejects_bad_cognitive_mode():
    """坏认知模式**丢弃整条**而不是降级成「观察」——降级＝用最宽松标签放行。"""
    result = parse_proposals(_proposal_json(cognitive_mode="猜测"))
    assert result["proposals"] == []
    assert result["discarded"]["bad_cognitive_mode"] == 1


def test_parse_requires_evidence_refs():
    """无证据的提案不进库（ADR-0002 证据纪律）。"""
    result = parse_proposals(_proposal_json(evidence_refs=[]))
    assert result["proposals"] == []
    assert result["discarded"]["no_evidence"] == 1


def test_parse_rejects_refs_outside_pool():
    """模型编造的编号不在本次证据池内 → 引用被过滤，无有效引用即丢弃。"""
    allowed = {"1": "chronicle:11", "2": "chronicle:12"}
    result = parse_proposals(_proposal_json(evidence_refs=["99"]), allowed_refs=allowed)
    assert result["proposals"] == []
    assert result["discarded"]["no_evidence"] == 1


def test_parse_maps_refs_through_pool():
    """编号 → chronicle 引用由**池**决定，不由模型给（模型只给编号）。"""
    allowed = {"1": "chronicle:11", "2": "chronicle:12"}
    result = parse_proposals(_proposal_json(evidence_refs=["2"]), allowed_refs=allowed)
    assert result["proposals"][0]["evidence_refs"] == ["chronicle:12"]


def test_parse_handles_markdown_fence_and_garbage():
    fenced = "```json\n" + _proposal_json() + "\n```"
    assert len(parse_proposals(fenced)["proposals"]) == 1
    assert parse_proposals("完全不是 JSON")["discarded"]["unparsable"] == 1
    assert parse_proposals("")["discarded"]["unparsable"] == 1


def test_parse_enforces_max_items():
    raw = json.dumps(
        [
            {
                "path": "perspective.world_view",
                "proposed_value": f"看法 {index}",
                "confidence": 0.8,
                "evidence_refs": ["chronicle:1"],
                "cognitive_mode": "观察",
            }
            for index in range(5)
        ],
        ensure_ascii=False,
    )
    result = parse_proposals(raw, max_items=2)
    assert len(result["proposals"]) == 2
    assert result["discarded"]["over_limit"] == 3


# ─── 证据收集 ───────────────────────────────────────────────────


def test_collect_evidence_applies_whitelist_and_mask():
    """白名单（life/daily 留下，promotion/diary 拒）+ general 掩码（限素材丢弃）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = NarrativeStore(Path(tmp))
        store.append_chronicle("self", "life", "通用片段")
        store.append_chronicle("self", "daily", "通用日结")
        store.append_chronicle("self", "diary", "日记")
        store.append_chronicle("self", "promotion", "晋升留痕")
        store.append_chronicle("self", "life", "涉私片段", source_uid="927386371")

        kinds = sorted(row["kind"] for row in collect_evidence(store))
        assert kinds == ["daily", "life"]


def test_pending_digest_excludes_evidence():
    """pending 摘要只有 id/status/path/value（HDSI 3.5）。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = NarrativeStore(Path(tmp))
        store.add_proposal(
            target="perspective",
            path="perspective.world_view",
            proposed_value="旧看法",
            confidence=0.9,
            evidence_refs="chronicle:1,chronicle:2",
        )
        digest = pending_digest(store)
        assert len(digest) == 1
        assert set(digest[0]) == {"id", "status", "path", "value"}


# ─── 主流程 ─────────────────────────────────────────────────────


def test_run_disabled():
    with tempfile.TemporaryDirectory() as tmp:
        runner, _store, _ = _make(tmp, enabled=False)
        assert asyncio.run(runner.run(now=NOW))["status"] == "disabled"


def test_run_insufficient_evidence_skips_llm_and_last_ts():
    """**关键**：证据不足不调 LLM，也**不写 last_ts**（否则 168h 内没机会触发）。"""
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, _ = _make(tmp, response=_pool_proposal_json())
        _seed_evidence(store, 2)  # < min_evidence_entries(5)

        result = asyncio.run(runner.run(now=NOW))
        assert result["status"] == "insufficient"
        assert runner._client.calls == [], "证据不足不得调用 LLM"
        assert store.get_kv_str("proposal:last_ts", "") == ""
        assert runner.is_due(NOW), "证据够了应立即有机会触发"


def test_run_success_persists_and_counts():
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, counter = _make(tmp, response=_pool_proposal_json())
        _seed_evidence(store, 6)

        result = asyncio.run(runner.run(now=NOW))
        assert result["status"] == "ok"
        assert result["proposals"] == 1
        assert len(store.list_proposals(status="pending")) == 1
        assert store.get_kv_str("proposal:last_ts", "") != ""
        assert counter.kinds == ["proposals"]
        # 温度用提案专用低温（要解 JSON）
        assert runner._client.calls[0]["temperature"] == _PROPOSAL.PROPOSAL_TEMPERATURE


def test_run_empty_proposal_still_writes_last_ts():
    """空提案也是结果（HDSI 5.3：不写 last_ts 会变成每 tick 白烧 token）。"""
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, _ = _make(tmp, response="[]")
        _seed_evidence(store, 6)

        result = asyncio.run(runner.run(now=NOW))
        assert result["status"] == "ok"
        assert result["proposals"] == 0
        assert store.get_kv_str("proposal:last_ts", "") != ""
        assert store.get_kv_int(f"proposal:empty:{NOW.date().isoformat()}", 0) == 1
        assert not runner.is_due(NOW), "刚跑过（含空提案）不得再判 due"


def test_run_not_due_within_interval():
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, _ = _make(tmp, response=_pool_proposal_json())
        _seed_evidence(store, 6)
        asyncio.run(runner.run(now=NOW))

        later = NOW + datetime.timedelta(hours=1)
        assert asyncio.run(runner.run(now=later))["status"] == "not_due"
        # 间隔到点后重新 due
        assert asyncio.run(
            runner.run(now=NOW + datetime.timedelta(hours=169))
        )["status"] == "ok"


def test_run_failure_sets_backoff_without_touching_last_ts():
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, _ = _make(tmp, fail=True)
        _seed_evidence(store, 6)

        result = asyncio.run(runner.run(now=NOW))
        assert result["status"] == "failed"
        assert store.get_kv_str("proposal:last_ts", "") == "", "失败不推进 last_ts"
        # 退避期内不再重试
        assert not runner.is_due(NOW + datetime.timedelta(hours=1))


def test_failure_fingerprint_not_persisted_across_restart():
    """**重启后必再试一次**（HDSI 5.3 显式设计决定：失败指纹不持久化）。"""
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, _ = _make(tmp, fail=True)
        _seed_evidence(store, 6)
        asyncio.run(runner.run(now=NOW))
        assert not runner.is_due(NOW + datetime.timedelta(hours=1))

        # 模拟重启：新建 runner（退避在内存，随旧实例消失）
        restarted, _, _ = _make(tmp, response=_pool_proposal_json())
        assert restarted.is_due(NOW + datetime.timedelta(hours=1))


def test_run_force_bypasses_due_check():
    with tempfile.TemporaryDirectory() as tmp:
        runner, store, _ = _make(tmp, response=_pool_proposal_json())
        _seed_evidence(store, 6)
        asyncio.run(runner.run(now=NOW))
        assert asyncio.run(runner.run(now=NOW, force=True))["status"] == "ok"


def test_run_skips_discarded_relationship_proposal():
    """端到端：LLM 返回 relationship 提案 → 落库为 0 条（只服务 perspective）。"""
    with tempfile.TemporaryDirectory() as tmp:
        raw = json.dumps(
            [
                {
                    "path": "relationship.closeness",
                    "proposed_value": "更亲近了",
                    "confidence": 0.9,
                    "evidence_refs": ["chronicle:1"],
                    "cognitive_mode": "观察",
                },
                json.loads(_pool_proposal_json())[0],
            ],
            ensure_ascii=False,
        )
        runner, store, counter = _make(tmp, response=raw)
        _seed_evidence(store, 6)

        result = asyncio.run(runner.run(now=NOW))
        assert result["proposals"] == 1
        assert result["discarded"]["path_not_allowed"] == 1
        stored = store.list_proposals(status="pending")
        assert [row["path"] for row in stored] == ["perspective.world_view"]


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
