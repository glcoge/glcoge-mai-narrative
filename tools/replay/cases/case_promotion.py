"""批 4-C11 晋升链路回放用例（三个）。

这三个用例回答的是**只有真实归档数据才能回答**的问题：

- ``promotion_rhythm``：拿 26 天真 chronicle 喂晋升机，产出**不为零**（功能没
  形同虚设）也**不过密**（门槛没被写松）→ 据此定标 P3/P4/P6/P7。
- ``input_sufficiency``：证据足够时**产出非空**（HDSI 5.9 Agency 恒空）；
  不足时连 LLM 都不调。
- ``positive_signal``：归档真 csv 按日去重后的场景日数落在实测区间内，
  且「≥3 日」用户占比 ≥ 6/7（关系确定性晋升的证据底座）。

数据来源一律 CLI 传入（``--db`` / ``--metrics-dir``），归档数据不入库。
⚠️ 失败信息里的用户号一律打码（脱敏约定：机制级可留、可识别细节模糊化）。
"""

from __future__ import annotations

import asyncio
import datetime
import math
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Dict, List

from .. import loader
from ..runtime import fixtures, load

# ── 定标常量：数字来自归档实测（2026-09-26 核事实），改动必须同步登记表 ──
#: 正向信号按日去重后的实测区间（7 个用户：19/13/9/9/8/4/2 天）
MEASURED_MIN_DAYS = 2
MEASURED_MAX_DAYS = 19
#: 「够用」的门槛与占比要求：6/7 用户 ≥ 3 天
ENOUGH_DAYS = 3
MIN_USERS_WITH_ENOUGH_DAYS = 6
#: 节奏上界的安全余量（一次模拟日的边界对齐误差）
RHYTHM_SLACK = 1


def _mask(uid: Any) -> str:
    """用户号打码（失败信息里不出现完整真实 QQ 号）。"""
    text = str(uid or "")
    if len(text) <= 4:
        return "***"
    return f"{text[:3]}***{text[-2:]}"


# ── 用例 1：晋升节奏（真实 chronicle） ──────────────────────────


def case_promotion_rhythm(inputs: loader.ReplayInputs) -> List[str]:
    """喂归档 chronicle → 断言晋升产出**不为零且不过密**。"""
    if not inputs.has_db():
        return [
            "未提供 --db（本用例需要归档 narrative.db；数据路径一律 CLI 传入）"
        ]

    evidence = load("services.learning.evidence")
    continuity = load("services.state.continuity")
    store_mod = load("services.store")
    engine_mod = load("services.state.engine")
    fx = fixtures()

    rows = loader.load_chronicle(inputs.db)
    eligible = evidence.eligible_entries(rows)
    if not eligible:
        return [f"归档 chronicle 里没有合法证据条目（共 {len(rows)} 行）——数据源不符预期"]

    days = sorted({str(row.get("ts"))[:10] for row in eligible if str(row.get("ts"))[:10]})
    cfg = fx.promotion_config()

    # ── 纯函数层：用真实数据算门槛两侧的数 ──
    scene_count, day_count = continuity.scene_stats(
        [
            {"ts": str(row.get("ts")), "source_uid": str(row.get("source_uid") or "")}
            for row in eligible
        ]
    )
    failures: List[str] = []
    if scene_count < 1 or day_count < 1:
        failures.append(f"场景统计异常：scenes={scene_count} days={day_count}")
    if day_count < int(cfg.minor_min_days):
        failures.append(
            f"归档只有 {day_count} 个跨日场景，低于门槛 {cfg.minor_min_days}——"
            "本用例无法验证晋升节奏（数据源问题，非机制问题）"
        )

    # ── 节奏模拟：逐日推进时间轴，冷却窗口决定能晋升几次 ──
    cooldown_days = max(1, float(cfg.cooldown_hours) / 24.0)
    rhythm_days: List[str] = []
    last_ts = ""
    for day in days:
        now = datetime.datetime.fromisoformat(f"{day}T12:00:00")
        if not continuity.is_after_cooldown(
            last_ts, now=now, cooldown_hours=float(cfg.cooldown_hours)
        ):
            continue
        if not continuity.meets_minor_gate(
            confidence=0.9,
            scene_count=scene_count,
            day_count=day_count,
            min_confidence=float(cfg.minor_confidence),
            min_scenes=int(cfg.minor_min_scenes),
            min_days=int(cfg.minor_min_days),
        ):
            continue
        rhythm_days.append(day)
        last_ts = now.isoformat(timespec="seconds")

    ceiling = int(math.ceil(len(days) / cooldown_days)) + RHYTHM_SLACK
    if len(rhythm_days) < 1:
        failures.append(
            "26 天真实素材一轮晋升都没发生（门槛过紧 / 时钟口径错）——"
            "这正是 HDSI 5.9「功能上线即形同虚设」的形态"
        )
    if len(rhythm_days) > ceiling:
        failures.append(
            f"晋升频率 {len(rhythm_days)} 次超过上界 {ceiling}"
            f"（{len(days)} 天 / 冷却 {cfg.cooldown_hours}h）——门槛被写松了"
        )

    # ── 端到端：真跑一次晋升机，确认产出真的落库（不只是纯函数算得对） ──
    # ⚠️ PERSPECTIVE_FIELDS 存的是**叶子名**（world_view），不是完整路径——
    # 慢变白名单要的是 ``perspective.world_view``，慢一步就会撞上 fail-closed。
    path = f"perspective.{continuity.PERSPECTIVE_FIELDS[0]}"
    with TemporaryDirectory() as tmp:
        store = store_mod.NarrativeStore(Path(tmp))
        for row in eligible:
            store.append_chronicle(
                str(row.get("scope") or "self"),
                str(row.get("kind")),
                str(row.get("text")),
                ts=str(row.get("ts")),
            )
        refs = ",".join(
            f"chronicle:{row['id']}" for row in store.list_chronicle_rows("self", limit=1000)
        )
        engine = _FakeEngine(engine_mod, fx)
        plugin = _plugin_stub(fx, store, engine)
        store.add_proposal(
            target="perspective",
            path=path,
            proposed_value="归档回放：她觉得世界比想象的大",
            confidence=0.9,
            status="pending",
            evidence_refs=refs,
        )
        row = store.list_proposals(status="pending", limit=1)[0]
        now = datetime.datetime.fromisoformat(f"{days[0]}T12:00:00")
        result = continuity.PromotionEngine(plugin).apply(row, now=now)
        if not result.get("applied"):
            failures.append(
                f"端到端晋升未落库（reason={result.get('reason')}）——"
                "纯函数算得对但执行路径没接上"
            )
        elif not store.list_promotions(path=path):
            failures.append("晋升报告成功但没有审计行（回滚依据缺失）")

    return failures


# ── 用例 2：输入充分性（合成输入） ──────────────────────────────


class _StubClient:
    """假 LLM：固定返回给定文本，并记录调用次数。"""

    def __init__(self, response: str = ""):
        self._response = response
        self.calls = 0

    async def generate(self, prompt: str, *, temperature=None) -> str:
        self.calls += 1
        return self._response


def case_input_sufficiency(inputs: loader.ReplayInputs) -> List[str]:
    """证据足够 → 产出**非空**；证据不足 → 连 LLM 都不调（HDSI 5.9 防恒空）。

    合成输入（不依赖归档）：本用例验的是**闸门两侧的行为**，与具体数据无关；
    用真实数据反而会被数据本身的变化掩盖结论。
    """
    del inputs
    evidence = load("services.learning.evidence")
    proposal_mod = load("services.learning.proposal")
    store_mod = load("services.store")
    fx = fixtures()

    cfg = fx.promotion_config()
    minimum = int(cfg.min_evidence_entries)
    failures: List[str] = []

    with TemporaryDirectory() as tmp:
        store = store_mod.NarrativeStore(Path(tmp))
        engine = _FakeEngine(load("services.state.engine"), fx)
        plugin = _plugin_stub(fx, store, engine)

        # ① 不足：不调 LLM、不写 last_ts
        for index in range(max(0, minimum - 1)):
            store.append_chronicle(
                "self", "life", f"素材 {index}", ts=f"2026-09-{10 + index:02d}T10:00:00"
            )
        runner = proposal_mod.ProposalRunner(plugin)
        runner._client = _StubClient("[]")
        low = asyncio.run(runner.run(now=datetime.datetime(2026, 9, 26, 12, 0, 0)))
        if low.get("status") != "insufficient":
            failures.append(f"证据不足时状态应为 insufficient，实为 {low.get('status')}")
        if runner._client.calls:
            failures.append("证据不足时不应调用 LLM（省钱 + 防恒空）")
        if store.get_kv_str("proposal:last_ts", ""):
            failures.append("证据不足时不应写 last_ts（否则 168h 内没机会触发）")

        # ② 充分：产出非空
        for index in range(max(0, minimum)):
            store.append_chronicle(
                "self", "life", f"补充素材 {index}", ts=f"2026-10-{10 + index:02d}T10:00:00"
            )
        runner._client = _StubClient(
            '[{"path": "perspective.world_view", "proposed_value": "她觉得世界很大",'
            ' "confidence": 0.8, "evidence_refs": ["1"], "cognitive_mode": "观察"}]'
        )
        high = asyncio.run(runner.run(now=datetime.datetime(2026, 10, 20, 12, 0, 0)))
        if high.get("status") != "ok":
            failures.append(f"证据充分时状态应为 ok，实为 {high.get('status')}")
        if int(high.get("proposals") or 0) < 1:
            failures.append(
                "证据充分却产出 0 条（HDSI 5.9 Agency 恒空的形态）"
            )
        if not store.list_proposals(status="pending"):
            failures.append("产出未落库")

    return failures


# ── 用例 3：正向信号（真实 csv） ────────────────────────────────


def case_positive_signal(inputs: loader.ReplayInputs) -> List[str]:
    """喂归档真 csv → 按日去重后的场景日数落在实测区间，且 6/7 用户 ≥3 天。"""
    if not inputs.has_metrics():
        return [
            "未提供 --metrics-dir（本用例需要归档 metrics/*.csv；数据路径一律 CLI 传入）"
        ]

    evidence = load("services.learning.evidence")
    store = loader.CsvMetricsStore.from_dir(inputs.metrics_dir)

    uids = set()
    for name in evidence.POSITIVE_SIGNAL_METRICS:
        for row in store.read_metrics(name):
            uid = str(row.get("user_id") or "").strip()
            if uid:
                uids.add(uid)
    if not uids:
        return ["归档 metrics 里没有任何正向信号记录——数据源不符预期"]

    counts = {uid: len(evidence.positive_signal_days(store, uid)) for uid in sorted(uids)}
    failures: List[str] = []
    for uid, count in counts.items():
        if count < MEASURED_MIN_DAYS:
            failures.append(
                f"用户 {_mask(uid)} 只有 {count} 个正向场景日（< 实测下限 {MEASURED_MIN_DAYS}）"
            )
        if count > MEASURED_MAX_DAYS:
            failures.append(
                f"用户 {_mask(uid)} 有 {count} 个正向场景日（> 实测上限 "
                f"{MEASURED_MAX_DAYS}）——去重口径可能失效（一天刷满门槛的形态）"
            )

    enough = sum(1 for count in counts.values() if count >= ENOUGH_DAYS)
    if enough < MIN_USERS_WITH_ENOUGH_DAYS:
        failures.append(
            f"仅 {enough}/{len(counts)} 个用户达到 {ENOUGH_DAYS} 个正向场景日"
            f"（要求 ≥ {MIN_USERS_WITH_ENOUGH_DAYS}）——关系确定性晋升会大面积不触发"
        )
    return failures


# ── 共用夹具 ────────────────────────────────────────────────────


class _FakeEngine:
    """晋升链路用的最小状态引擎（回放台不建真库的 state 层）。"""

    def __init__(self, engine_mod: Any, fx: Any):
        self._default_self = engine_mod.default_self_state
        self._default_branch = engine_mod.default_branch_state
        self.self_state = engine_mod.default_self_state()
        self.branches: Dict[str, Any] = {}

    def load_self_state(self):
        return self.self_state

    def save_self_state(self, state):
        self.self_state = state

    def load_branch_state(self, uid):
        return self.branches.setdefault(str(uid), self._default_branch())

    def save_branch_state(self, uid, state):
        self.branches[str(uid)] = state


def _plugin_stub(fx: Any, store: Any, engine: Any) -> Any:
    """最小 plugin 假件（config 默认值复用测试夹具，不另起第二份）。"""
    return SimpleNamespace(
        _store=store,
        _engine=engine,
        _telemetry=None,
        config=SimpleNamespace(
            promotion=fx.promotion_config(),
            plugin=SimpleNamespace(enabled=True),
            narrative=fx.sleep_config(sleep_time="", wake_time=""),
            llm=SimpleNamespace(
                creation_model="", temperature=0.9, creation_max_tokens=1024, show_prompt=False
            ),
        ),
        ctx=SimpleNamespace(logger=fx.null_logger()),
    )
