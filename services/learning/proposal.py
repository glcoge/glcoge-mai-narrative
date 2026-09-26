"""慢变提案通道（v0.2.0 批 4 · C3）：周度 LLM 提炼 → 受控维度契约 → 落库。

本模块把「LLM 可以对她的看法提建议」这件事收敛成一条**有闸门的管道**：

    chronicle 原文 ──【掩码】白名单 kind + general 目标丢受限素材──▶ 证据池
                     ──【输入充分性】条目数 < 阈值 直接不调 LLM（省钱 + 防恒空）
                     ──【LLM 提炼】HDS 负面清单把「本场戏证据」挡在外面
                     ──【契约校验】白名单外 path 丢弃 / confidence clamp /
                        认知模式枚举 / **证据引用必须落在本次证据池内**
                     ──▶ proposals 表（status=pending）

流程纪律（全部有对应测试）：

- **只服务 perspective**：relationship 走 C4 的确定性通道，**不进 LLM 提案**——
  配对语义批 4 冻结（R31），证据纪律 4 不满足就不喂模型。
- **pending 只回传 id/status/path/value**（HDSI 3.5）：``evidence`` 不回传，
  防模型看到自己旧候选后「凑证据」（自证循环）。
- **空提案也是结果**：产出 0 条也写 ``proposal:last_ts``，否则「无记录恒判 due」
  会变成每 tick 白烧 token 的重试风暴（HDSI 5.3）。
- **失败退避不持久化**：内存记 ``_backoff_until``，重启即清 → **重启后必再试一次**
  （HDSI 5.3 的显式设计决定：失败指纹不该跨重启，否则一次瞬时故障永久静音一个功能）。
- **输入不充分不写 last_ts**：证据一旦够了要**立刻**有机会触发，不能被 168h 的
  间隔闸门锁在门外。
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, List, Optional

from ..creation.creator import CreatorClient
from ..state.continuity import (
    PERSPECTIVE_FIELDS,
    is_slow_field,
    normalize_perspective,
    slow_set,
)
from .evidence import is_valid_cognitive_mode, mask_evidence

#: 提案提炼温度：要解 JSON，比创作层（0.9）低得多。
PROPOSAL_TEMPERATURE = 0.2

#: 本次提炼送去过目的证据条数上限（防 prompt 无界增长）。
EVIDENCE_LIMIT = 120

#: pending 提案回传上限（只供反证引用，不需要多）。
PENDING_DIGEST_LIMIT = 10

#: kv 键
_LAST_TS_KEY = "proposal:last_ts"
_EMPTY_PREFIX = "proposal:empty:"

#: 只服务的维度前缀（relationship 不进 LLM 提案）。
_PROPOSAL_SCOPE = "perspective"

#: HDS 负面清单（路线图批 4 步骤 1 原文）。写成常量便于测试断言「prompt 里确实有它」。
NEGATIVE_CHECKLIST = (
    "口吻 / 回复模式 / 昵称 / 散文节奏 / 暂时性情绪\n"
    "    —— 这些是本场戏的证据，**不是**发展倾向，不要写成她的长期看法。"
)


def build_proposal_prompt(
    entries: List[Dict[str, Any]],
    *,
    pending: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """组装提炼 prompt：负面清单 + 编号证据 + 已有候选（仅反证用） + 输出契约。

    证据**按编号呈现**，并要求 ``evidence_refs`` 用编号——模型照抄编号比让它自己
    生成 ``chronicle:<id>`` 字符串可靠得多；最终仍由 :func:`parse_proposals`
    做池内校验（不信任模型）。
    """
    lines: List[str] = [
        "你在维护一位长期相处的角色「她」的**慢变看法**：她对世界的看法与人生目标。",
        "",
        "【只提炼长期倾向】",
        "  只写「她大概会怎么看 / 想要什么」这类换个场景仍然成立的东西。",
        "  以下**不要**提炼：",
        f"  {NEGATIVE_CHECKLIST}",
        "  证据不够就返回空数组，不要硬凑、不要把一次事件写成她的性格。",
        "",
        "【证据】（只能引用下面的编号，不得引用你自己的推断）",
    ]
    index: Dict[str, str] = {}
    for position, entry in enumerate(entries, start=1):
        text = str(entry.get("text") or "").strip().replace("\n", " ")
        index[str(position)] = f"chronicle:{entry.get('id')}"
        lines.append(f"  [{position}] {text[:160]}")
    if not entries:
        lines.append("  （无）")

    if pending:
        lines += [
            "",
            "【已有候选】（**只**用于「反证」时按 id 引用；不要重复提出同样的东西）",
        ]
        for item in pending:
            lines.append(
                f"  - id={item.get('id')} path={item.get('path')} value={item.get('value')}"
            )
    else:
        lines += ["", "【已有候选】（无）"]

    allowed = "perspective.world_view（或 perspective.life_goals）"
    example = (
        '[{"target": "perspective", '
        '"path": "perspective.world_view", '
        '"proposed_value": "一句话，具体、可读", '
        '"confidence": 0.0, '
        '"evidence_refs": ["1", "3"], '
        '"cognitive_mode": "观察|转述|信念|提议|条件|确认", '
        '"holder": "她", '
        '"contradicts": []}]'
    )
    lines += [
        "",
        "【输出】严格 JSON 数组（不要 markdown 代码块、不要解释）：",
        f"  path 只能取 {allowed}；",
        f"  {example}",
        "  ``evidence_refs`` 填证据编号；``contradicts`` 填【已有候选】的 id（没有就留空数组）。",
        "  没有值得记的就输出 []。",
    ]
    return "\n".join(lines)


def _extract_json_array(text: str) -> Optional[List[Any]]:
    """从（可能带 markdown 包裹的）模型输出里抽出 JSON 数组。

    **无法解析时返回 ``None``**（而非空列表）——空列表是「模型说了没有提案」这一
    合法结果，与「模型输出根本不是 JSON」必须区分：前者写 ``last_ts``，后者算失败
    走退避。混为一谈会让解析故障伪装成「她最近没有新想法」。
    """
    normalized = str(text or "")
    start = normalized.find("[")
    end = normalized.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(normalized[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, list) else None


def _clamp_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _index_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _contradicts_list(value: Any) -> List[int]:
    result: List[int] = []
    for item in value if isinstance(value, list) else []:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def parse_proposals(
    raw_text: str,
    *,
    max_items: int = 8,
    allowed_refs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """把模型输出解析成受控提案列表。

    Returns:
        ``{"proposals": [...], "discarded": {...原因: 条数}}`` —— 丢弃原因分类计数
        是**可观测性**：不然「明明是通道通了却一条不落库」将无从排查。

    丢弃规则（任一命中即丢整条）：
    - ``path`` 不在慢变白名单 / 不在 ``perspective`` 域
    - ``proposed_value`` 为空
    - ``cognitive_mode`` 不在枚举内（**不降级**为「观察」——那是最宽松的标签，
      降级等于放行，而 ``条件``/``提议`` 被升格正是 HDSI 2.1 的事故形态）
    - ``evidence_refs`` 过滤后为空（ADR-0002 证据纪律：无证据的提案不进库）
    """
    discarded: Dict[str, int] = {}
    proposals: List[Dict[str, Any]] = []

    def _drop(reason: str) -> None:
        discarded[reason] = discarded.get(reason, 0) + 1

    entries = _extract_json_array(raw_text)
    if entries is None:
        _drop("unparsable")
        return {"proposals": [], "discarded": discarded}

    for item in entries:
        if len(proposals) >= max(1, max_items):
            _drop("over_limit")
            continue
        if not isinstance(item, dict):
            _drop("not_object")
            continue
        path = str(item.get("path") or "").strip()
        if not is_slow_field(path) or path.split(".", 1)[0] != _PROPOSAL_SCOPE:
            _drop("path_not_allowed")
            continue
        value = str(item.get("proposed_value") or "").strip()
        if not value:
            _drop("empty_value")
            continue
        mode = str(item.get("cognitive_mode") or "").strip()
        if not is_valid_cognitive_mode(mode):
            _drop("bad_cognitive_mode")
            continue

        raw_refs = _index_list(item.get("evidence_refs"))
        if allowed_refs is not None:
            refs = [allowed_refs[key] for key in raw_refs if key in allowed_refs]
        else:
            refs = [text for text in raw_refs if text.startswith("chronicle:")]
        if not refs:
            _drop("no_evidence")
            continue

        proposals.append(
            {
                "target": _PROPOSAL_SCOPE,
                "path": path,
                "proposed_value": value,
                "confidence": _clamp_confidence(item.get("confidence")),
                "cognitive_mode": mode,
                "holder": str(item.get("holder") or "").strip(),
                "evidence_refs": refs,
                "contradicts": _contradicts_list(item.get("contradicts")),
            }
        )
    return {"proposals": proposals, "discarded": discarded}


def collect_evidence(store: Any, *, limit: int = EVIDENCE_LIMIT) -> List[Dict[str, Any]]:
    """收集 perspective 的合法证据（白名单 + 来源桶掩码），按 ``id`` 去重。

    对 ``PERSPECTIVE_FIELDS`` **逐个** 过掩码（当前两者受众都是 general，结果等价）
    ——显式逐个走一遍，将来某维度改受众时不会静默沿用旧掩码。
    """
    rows = store.list_chronicle_rows("self", limit=limit)
    pool: Dict[str, Dict[str, Any]] = {}
    for field in PERSPECTIVE_FIELDS:
        for row in mask_evidence(rows, target=f"perspective.{field}"):
            pool[str(row.get("id"))] = row
    return list(pool.values())


def pending_digest(store: Any, *, limit: int = PENDING_DIGEST_LIMIT) -> List[Dict[str, Any]]:
    """列出待决提案的**最小摘要**（HDSI 3.5：``evidence`` 不回传）。

    回传完整 evidence 会让模型看见自己上一轮凑的"证据"并据此自我确认
    （HDSI 1.10 温水杯：结构化产物循环喂回模型 → 自我强化）。
    """
    rows = store.list_proposals(status="pending", limit=limit)
    return [
        {
            "id": row.get("id"),
            "status": row.get("status"),
            "path": row.get("path"),
            "value": row.get("proposed_value"),
        }
        for row in rows
    ]


class ProposalRunner:
    """周度提案提炼器（失败退避在内存，重启即清）。

    ``now`` 一律由调用方传入（plugin 的 ``_local_now()``）——本模块**不 import
    engine**，避免与状态层形成循环依赖；同时让测试能确定性地控制时间。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._client = CreatorClient(plugin)
        #: 失败退避截止时刻。**刻意只在内存**：重启即清 → 重启后必再试一次
        #: （HDSI 5.3 显式设计决定：失败指纹不持久化）。
        self._backoff_until: Optional[datetime.datetime] = None
        #: 上次提炼的结果摘要（供 /narrative status 展示与断言）。
        self.last_result: Dict[str, Any] = {}

    # ─── 调度判定 ───────────────────────────────────────────────

    def _last_ts(self) -> str:
        store = self._plugin._store
        if store is None:
            return ""
        return store.get_kv_str(_LAST_TS_KEY, "")

    def _interval_hours(self) -> float:
        return float(self._plugin.config.promotion.interval_hours or 168)

    def is_due(self, now: datetime.datetime) -> bool:
        """现在是否该提炼：未退避 AND （从未提炼过 OR 距上次 ≥ 间隔）。"""
        if self._backoff_until is not None and now < self._backoff_until:
            return False
        last_text = self._last_ts()
        if not last_text:
            return True
        try:
            last = datetime.datetime.fromisoformat(last_text)
        except ValueError:
            return True
        return (now - last).total_seconds() >= self._interval_hours() * 3600

    # ─── 主流程 ─────────────────────────────────────────────────

    async def run(
        self, *, now: datetime.datetime, force: bool = False
    ) -> Dict[str, Any]:
        """跑一次提炼。返回结果摘要（同时存进 ``last_result``）。

        返回 ``status`` 取值：``disabled`` / ``not_due`` / ``insufficient`` /
        ``failed`` / ``ok``。
        """
        promotion = self._plugin.config.promotion
        store = self._plugin._store
        if not promotion.enabled or store is None:
            return self._record({"status": "disabled"})
        if not force and not self.is_due(now):
            return self._record({"status": "not_due"})

        evidence = collect_evidence(store)
        minimum = int(promotion.min_evidence_entries or 1)
        if len(evidence) < minimum:
            # ⚠️ 刻意**不写 last_ts**：证据一旦够了要立刻有机会触发，
            # 不能被 168h 的间隔闸门锁在门外（HDSI 5.9 防「功能上线即形同虚设」）。
            return self._record(
                {"status": "insufficient", "evidence": len(evidence), "minimum": minimum}
            )

        # 证据池的编号 → chronicle 引用，供契约校验用（不信任模型给的 id）
        allowed_refs = {
            str(position): f"chronicle:{row.get('id')}"
            for position, row in enumerate(evidence, start=1)
        }
        prompt = build_proposal_prompt(
            evidence, pending=pending_digest(store)
        )
        raw = await self._client.generate(prompt, temperature=PROPOSAL_TEMPERATURE)
        if not raw:
            backoff_hours = float(promotion.failure_backoff_hours or 6)
            self._backoff_until = now + datetime.timedelta(hours=backoff_hours)
            # ⚠️ 不同步 last_ts：重启后（退避清空 + last_ts 仍是旧值）必然再试一次
            return self._record({"status": "failed", "evidence": len(evidence)})

        parsed = parse_proposals(
            raw, max_items=int(promotion.max_proposals or 8), allowed_refs=allowed_refs
        )
        saved_ids: List[int] = []
        for proposal in parsed["proposals"]:
            proposal_id = store.add_proposal(
                target=proposal["target"],
                path=proposal["path"],
                proposed_value=proposal["proposed_value"],
                confidence=proposal["confidence"],
                status="pending",
                evidence_refs=",".join(proposal["evidence_refs"]),
            )
            saved_ids.append(proposal_id)
            self._count("proposals")

        # 空提案也写 last_ts（HDSI 5.3：防「无记录恒判 due」的重试风暴）
        store.set_kv_str(_LAST_TS_KEY, now.isoformat(timespec="seconds"))
        if not saved_ids:
            store.set_kv_int(f"{_EMPTY_PREFIX}{now.date().isoformat()}", 1)
        return self._record(
            {
                "status": "ok",
                "evidence": len(evidence),
                "proposals": len(saved_ids),
                "proposal_ids": saved_ids,
                "discarded": parsed["discarded"],
            }
        )

    # ─── 内部 ───────────────────────────────────────────────────

    def _count(self, kind: str) -> None:
        telemetry = self._plugin._telemetry
        if telemetry is not None:
            telemetry.record_counter(kind)

    def _record(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.last_result = dict(result)
        return result


# ─── 冷启动 seed（批 4-C5 / R15） ───────────────────────────────


def build_seed_prompt(world: str, values: List[str]) -> str:
    """组装 seed prompt：从锚定层派生 ``world_view`` / ``life_goals`` 初值。

    措辞刻意强调「初始基线、可被推翻」——seed 不是补充人设，是给晋升 diff 一个
    起点（不设 seed 则「现值」不存在，「她的看法何时、因何证据变过」无从叙述）。
    """
    lines = ["下面是这个角色在世界观与价值观上的设定。请据此写她**最初的**看法。", ""]
    lines.append("【世界观设定】")
    lines.append(f"  {world.strip() or '（未填写）'}")
    lines.append("")
    lines.append("【价值观底线】")
    if values:
        for value in values:
            lines.append(f"  - {str(value).strip()}")
    else:
        lines.append("  （未填写）")
    lines += [
        "",
        "【要求】",
        "  这是她的**初始基线**，此后会随相处慢慢变化——写得朴素、具体、可被推翻，",
        "  不要写成不可动摇的铁律，也不要重复上面设定的原句。",
        "  人生目标给 1~3 条，每条一句话。",
        "",
        "【输出】严格 JSON 对象（不要 markdown 代码块、不要解释）：",
        '  {"world_view": "她对世界的一句看法", "life_goals": ["目标一", "目标二"]}',
    ]
    return "\n".join(lines)


def parse_seed(raw_text: str) -> Optional[Dict[str, Any]]:
    """解析 seed 输出；解析不出或形状不对返回 ``None``。"""
    normalized = str(raw_text or "")
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(normalized[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    world_view = str(payload.get("world_view") or "").strip()
    raw_goals = payload.get("life_goals")
    goals = [
        str(item).strip()
        for item in (raw_goals if isinstance(raw_goals, list) else [])
        if str(item).strip()
    ]
    if not world_view and not goals:
        return None
    return {"world_view": world_view, "life_goals": goals}


def _perspective_is_empty(perspective: Dict[str, Any]) -> bool:
    """未播种判定：``origin`` 为空且两个维度都没有内容。"""
    if str(perspective.get("origin") or "").strip():
        return False
    if str(perspective.get("world_view") or "").strip():
        return False
    return not (perspective.get("life_goals") or [])


async def seed_perspective(plugin: Any, *, now: datetime.datetime) -> Dict[str, Any]:
    """冷启动播种（R15）：从锚定层一次性派生 perspective 初值，标 ``origin=seed``。

    返回 ``status``：``disabled`` / ``already`` / ``no_anchor`` / ``failed`` /
    ``empty`` / ``ok``。

    ⚠️ 失败**不阻断**启动（同 R14 锚定一致性比对口径）：留空 + 由调用方 WARN，
    下次启动会再试（未播种状态不会被跳过）。
    """
    promotion = plugin.config.promotion
    store = plugin._store
    if store is None or plugin._engine is None:
        return {"status": "disabled"}
    if not promotion.enabled or not promotion.seed_on_start:
        return {"status": "disabled"}

    engine = plugin._engine
    state = engine.load_self_state()
    perspective = normalize_perspective(state)
    if not _perspective_is_empty(perspective):
        return {"status": "already"}

    identity = getattr(plugin.config, "identity", None)
    world = str(getattr(identity, "world", "") or "").strip()
    values = [str(item) for item in (getattr(identity, "values", None) or [])]
    if not world and not values:
        # 锚定层没配 → 无从派生（不是错误，用户可能还没填 [identity]）
        return {"status": "no_anchor"}

    client = CreatorClient(plugin)
    raw = await client.generate(
        build_seed_prompt(world, values), temperature=PROPOSAL_TEMPERATURE
    )
    if not raw:
        return {"status": "failed"}
    parsed = parse_seed(raw)
    if parsed is None:
        return {"status": "empty"}

    slow_set(state, "perspective.world_view", parsed["world_view"], actor="seed")
    slow_set(state, "perspective.life_goals", parsed["life_goals"], actor="seed")
    perspective["origin"] = "seed"
    perspective["updated_ts"] = now.isoformat(timespec="seconds")
    engine.save_self_state(state)
    return {
        "status": "ok",
        "world_view": parsed["world_view"],
        "life_goals": parsed["life_goals"],
    }


__all__ = [
    "EVIDENCE_LIMIT",
    "NEGATIVE_CHECKLIST",
    "PENDING_DIGEST_LIMIT",
    "PROPOSAL_TEMPERATURE",
    "ProposalRunner",
    "build_proposal_prompt",
    "build_seed_prompt",
    "collect_evidence",
    "parse_proposals",
    "parse_seed",
    "pending_digest",
    "seed_perspective",
]
