"""由头取材（sourcing）：主动消息"说什么"的来源，以及分享欲的奖惩。

从 ``state/engine.py`` 拆出（v0.2.0 批 1，执行路线「类拆分推迟表」）：
``build_bysource`` / ``_bysource_used_key`` / ``compute_share_urge`` / ``record_urge_feedback``。
归到 proactive 包是因为它们只服务**主动开口**，与"世界推进"无关——engine 已经
900+ 行，主动链路的逻辑不该再往里堆。

依赖方向：本模块接受 ``engine`` 实例（不持有状态），engine 侧保留一层薄委托方法，
对外 API 不变（``engine.build_bysource`` 等照旧）。

⚠ 循环导入：``state/engine.py`` 会在模块顶层 import 本模块，故本模块**不得**在顶层
import engine。``INJECT_TEXT_CAP`` 采用函数内延迟导入（见 ``build_bysource``）。
``continuity`` 是纯声明模块（零上层依赖），可以顶层 import。
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..render.audience import filter_entries
from ..state.continuity import current_relationship_stage

# 由头去复用：已用作由头的生活片段 ts 集合（逗号分隔，有界 8 条）
# 按 user_id 命名空间隔离——store.get_kv_str 没有 scope 参数，而生活片段挂在
# 自我层（全局共享）：若所有用户共用一个键，先触发的用户会把素材用尽，
# 后触发的用户取不到由头 → build_bysource 返回空 → 本轮主动开口被跳过。
# 同一件事讲给不同朋友听是自然的，所以去复用只约束同一段关系。
_BYSOURCE_USED_KEY_PREFIX = "bysource:used:"

#: 由头与最近对话的文本重叠上限（OBSERVE(R23)）：超过则判定"撞车"，换一个候选。
#: 中文无词边界，用**字符 bigram 的 Jaccard 相似度**——零依赖、离线可重算。
#: 取值先拍一个保守值，真机跑一轮后按回放台数据调。
#: OBSERVE(P2)：0.5 是批 1 观察值（字符 bigram Jaccard）；调高了由头变复述，调低了可用候选枯竭。
_OVERLAP_REJECT = 0.5

# 里程碑消费三闸（方案 §6.3 / Q10b / OBSERVE(P21)）：记忆要能想起，但不能翻来覆去。
# ① 条目冷却 —— 同一件事短期内不反复拿来讲；
# ② 保质期 —— 「我早就忘了，她还天天提」比不提更糟，过期旧事不再提取；
# ③ 3 选 1 节流 —— 防回忆素材挤占日常素材，否则她的生活只剩「回忆」。
# 三值均为模块常量（方案 §15.1 的 3+9 拆分）：观察期内物理上改不动。
_MILESTONE_COOLDOWN_DAYS = 7
_MILESTONE_TTL_DAYS = 30
_MILESTONE_THROTTLE_WINDOW = 3
#: 已消费里程碑登记表（per-uid JSON ``{id: iso_ts}``）——条目级冷却，有界同上限。
_MILESTONE_USED_KEY_PREFIX = "milestone:consumed:"
#: 最近若干次由头的来源队列（per-uid ``"fragment,milestone,mood"``），供 3 选 1 节流。
_ORIGIN_KEY_PREFIX = "bysource:origin:"

#: 每用户已消费登记表条数上限（与 milestones 保留上限同宽即可：
#: 被登记的条目一定来自该用户的 milestones，条目被挤出时登记也已无意义）。
_MILESTONE_USED_KEEP = 20


@dataclass
class _BySourceCandidate:
    """一个由头候选。

    - ``ts``：生活片段的时间戳（**空串＝非片段来源**，不进由头去复用表）；
    - ``origin``：``fragment`` / ``milestone`` / ``mood`` —— 供 3 选 1 节流统计；
    - ``milestone_id``：仅 ``origin="milestone"`` 时非空，用于条目级冷却登记。
    """

    text: str
    ts: str = ""
    origin: str = "fragment"
    milestone_id: str = ""


def _bysource_used_key(user_id: str) -> str:
    """某用户已用作由头的片段 ts 集合的 kv 键（按 uid 隔离）。"""
    return f"{_BYSOURCE_USED_KEY_PREFIX}{user_id}"


def _parse_iso(value: str) -> Optional[datetime.datetime]:
    """解析 ISO 时间戳；失败返回 None（调用方一律按 fail-closed 处理）。

    条目 ts 由我们自己写入（``datetime.isoformat()``），但归档库里可能有旧形状数据，
    **解析不出就不用它** —— 宁可少讲一件旧事，不可拿捏不准的时间做减法。
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _recent_origins(engine: Any, user_id: str) -> List[str]:
    """最近若干次由头的来源队列（最旧在前，最多 ``_MILESTONE_THROTTLE_WINDOW`` 项）。"""
    raw = engine._store.get_kv_str(f"{_ORIGIN_KEY_PREFIX}{user_id}") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _milestone_consumed_map(engine: Any, user_id: str) -> Dict[str, str]:
    """该用户已消费里程碑的登记表 ``{milestone_id: 上次消费 iso_ts}``（坏数据退空表）。"""
    raw = engine._store.get_kv_str(f"{_MILESTONE_USED_KEY_PREFIX}{user_id}") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _bigrams(text: str) -> set:
    """字符 bigram 集合（中文无空格，bigram 比分词更稳且零依赖）。"""
    normalized = "".join(ch for ch in str(text or "") if not ch.isspace())
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i : i + 2] for i in range(len(normalized) - 1)}


def overlap_ratio(left: str, right: str) -> float:
    """两段文本的字符 bigram Jaccard 相似度 ∈ [0,1]（空文本返回 0）。"""
    left_set = _bigrams(left)
    right_set = _bigrams(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def _recent_dialogue_text(engine: Any, user_id: str, limit: int = 5) -> str:
    """该用户最近的对话原文（用于由头撞车判定），失败降级为空串。"""
    try:
        events = engine._store.list_events(f"branch:{user_id}", limit=limit)
    except Exception:  # noqa: BLE001 —— 取材增强不得影响主动开口主链路
        return ""
    return "\n".join(str(item.get("bysource", "") or "") for item in events)


# ─── 由头签发 ──────────────────────────────────────────────────


def build_bysource(
    engine: Any, user_id: str, now: Optional[datetime.datetime] = None
) -> str:
    """从"bot 自己的生活"签发主动开口的由头（v0.1.3 重构）。

    取材优先级（全部来自 bot 自身，禁止取材用户消息镜像，防复述）：
    1. 创作层生活片段（focus.pending_events 全窗口，容量见 fragment_pending_max）；
    2. 支线里程碑（你们之间发生过的事，受**消费三闸**约束）；
    3. 情绪/作息（疲惫想倾诉、深夜清醒）。

    **详略与资格解耦**（2026-09-30 回滚批 1 的 minor 排除，Q1）：任何非空
    片段都可作由头，不设档位资格门；真正的取材优先级是 ① 未用过（同一段
    关系内去复用）+ ② 与最近对话不撞车（文本重叠，R23）。高光片段（Q12）
    只作 **2 倍权重加成**（占槽实现），同样不设资格门。

    **里程碑三闸**（方案 §6.3 / Q10b）：① 条目 7 天冷却 ② 30 天保质期
    ③ 最近 3 次由头里最多 1 次来自里程碑。`stage != "陌生人"` 门槛（Q10c）保留。

    没有可用素材时返回空串——上层应**跳过本次主动开口**，而不是发干聊。
    """
    from ..state.engine import INJECT_TEXT_CAP  # 延迟导入：避开 engine ↔ sourcing 循环

    current = now or engine._local_now()
    cfg = engine._plugin.config
    state = engine.load_self_state()
    branch = engine.load_branch_state(user_id)

    # 去复用：按用户读取已用记录（同一件事可以讲给不同朋友听，只对同一段关系去重）
    used_raw = engine._store.get_kv_str(_bysource_used_key(user_id))
    used = {item.strip() for item in (used_raw or "").split(",") if item.strip()}
    recent_text = _recent_dialogue_text(engine, user_id)

    # 候选列表；ts 仅片段来源非空（用于选中后登记"已用"），origin 供 3 选 1 节流统计
    candidates: List[_BySourceCandidate] = []
    # 受众过滤（ADR-0004）：生活片段默认通用（无标签），但被打标就必须按受众隔离
    pending = filter_entries(state["state"]["focus"].get("pending_events", []), user_id)
    # 全窗口取材（方案 §7 / P20）：窗口与容量同宽，不再只取最近两条
    for item in pending:
        fragment = str(item.get("text", "") or "").strip()
        if not fragment:
            continue
        ts = str(item.get("ts", "") or "").strip()
        # 去复用：同一片段不作二次由头
        if ts and ts in used:
            continue
        # 与最近对话撞车则跳过（OBSERVE(R23)）：由头要是"新事"，不是刚聊过的复述
        if recent_text and overlap_ratio(fragment, recent_text) >= _OVERLAP_REJECT:
            continue
        # 高光 2 倍权重（Q12 / §4.2）：第二次入列 = 占槽实现，保持确定性选择；
        # 只加权不设资格门；重复入列不产生二次登记（used.add 与去重查询皆幂等）。
        entry = _BySourceCandidate(text=f"最近一段生活：{fragment[:INJECT_TEXT_CAP]}", ts=ts)
        candidates.append(entry)
        if item.get("highlight"):
            candidates.append(entry)

    # 关系里程碑取自 relationship 命名空间（批 2 四维 schema）；stage 缺省时由
    # 只读事实推导（continuity），不再读被删的 familiarity 规则线。
    # 消费三闸（方案 §6.3 / Q10b）：3 选 1 节流 → 保质期 → 条目冷却，任一不过即不取。
    relationship = branch.get("relationship", {})
    milestones = list(relationship.get("milestones", []))
    stage = str(
        relationship.get("stage")
        or current_relationship_stage({"milestones": milestones})
    )
    if milestones and stage != "陌生人":
        origins = _recent_origins(engine, user_id)
        consumed = _milestone_consumed_map(engine, user_id)
        # ③ 3 选 1 节流：最近 3 次由头里已有 milestone → 本轮不取回忆（防茧房：
        #    否则「她的生活」会退化成只剩往事）。查在遍历之前，省一次无谓循环。
        if "milestone" not in origins:
            for item in reversed(milestones):
                m_ts = _parse_iso(str(item.get("ts", "") or ""))
                if m_ts is None:
                    continue  # 无 ts / 解析不出：无法判保质 → fail-closed 跳过
                # ② 保质期：过期旧事不再提取
                if (current - m_ts).days > _MILESTONE_TTL_DAYS:
                    continue
                m_id = str(item.get("id", "") or "")
                # ① 条目冷却：这件往事最近讲过 → 换下一件（不是整个放弃）
                last = _parse_iso(str(consumed.get(m_id, "") or ""))
                if last is not None and (current - last).days < _MILESTONE_COOLDOWN_DAYS:
                    continue
                candidates.append(
                    _BySourceCandidate(
                        text=f"想起我们之间那件事：{str(item.get('desc', '') or '')}",
                        origin="milestone",
                        milestone_id=m_id,
                    )
                )
                break  # 只取最新一条可用条目：候选多一条也只会被取模选中一条

    if not candidates:
        mood = str(state["state"]["mood"].get("label", "平静"))
        if mood in ("低落", "疲惫"):
            candidates.append(
                _BySourceCandidate(text=f"今天有点{mood}，想找人聊聊", origin="mood")
            )
        routine = state["state"].get("routine", {})
        phase = str(routine.get("phase", ""))
        # 睡着就别说「还不想睡」——睡眠态上线后这条只在清醒的深夜才成立
        # （实际上深夜既在静默期又在睡眠窗口内，本分支基本不可达，留着只为
        #  日后把静默期调窄时语义仍然正确）
        if phase == "深夜" and str(routine.get("sleep_state", "awake")) != "asleep":
            candidates.append(
                _BySourceCandidate(text="夜深了，我还不想睡，想跟你说点什么", origin="mood")
            )
        elif phase == "清晨":
            candidates.append(
                _BySourceCandidate(text="刚醒，今天莫名的想先跟你说句话", origin="mood")
            )

    if not candidates:
        return ""  # 无可借由的生活素材：本轮主动取消（宁可缺席，不干聊）

    # 场景感补全：确定性选一个（按小时稳定），避免同一天重复同一由头
    seed = sum(ord(char) for char in user_id) + current.hour + (current.date().day * 7)
    chosen = candidates[seed % len(candidates)]
    if chosen.ts:
        # 登记已用（宽度与素材池同宽，派生自 fragment_pending_max，不设独立配置）：
        # 下次该片段不再作由头（仅对该用户生效）。上限与 pending 同宽即足够——
        # 片段被挤出登记表之前必然先被挤出素材池（两者同为「取最新 N 条」的 LRU，
        # 且被登记的 ts 一定是素材池里待过的片段），不存在「还在池里却查不到已用」
        # 的窗口。
        cap = max(1, int(cfg.narrative.fragment_pending_max))
        used.add(chosen.ts)
        engine._store.set_kv_str(_bysource_used_key(user_id), ",".join(sorted(used)[-cap:]))
    if chosen.origin == "milestone" and chosen.milestone_id:
        # 条目级冷却登记（选中即登记，与"已用"同步：未送达也照样冷却，
        # 兜底预案见方案 §4.3 —— 若 L1「选中未送达率」>20% 再改为送达时登记）
        consumed[chosen.milestone_id] = current.isoformat(timespec="seconds")
        engine._store.set_kv_str(
            f"{_MILESTONE_USED_KEY_PREFIX}{user_id}",
            json.dumps(dict(list(consumed.items())[-_MILESTONE_USED_KEEP:]), ensure_ascii=False),
        )
    # 来源队列（3 选 1 节流的输入）：无论哪一路来源都记，窗口滑动保留最近 N 次
    origins = (_recent_origins(engine, user_id) + [chosen.origin])[-_MILESTONE_THROTTLE_WINDOW:]
    engine._store.set_kv_str(f"{_ORIGIN_KEY_PREFIX}{user_id}", ",".join(origins))
    return chosen.text


# ─── 分享欲 share_urge（v0.1.8 第一步：动机驱动主动时机） ──────


def record_urge_feedback(engine: Any, user_id: str, event: str) -> None:
    """分享欲事件反馈（规则层零 LLM，第一步方案）。

    event 取值：
    - ``"caught"``：主动消息在承接窗口（16h）内被接住 → self/branch 双升（聊得起来，更想聊）；
    - ``"ignored"``：主动消息**已确认送达**但超窗无人接住 → self/branch 双降（别热脸贴冷屁股）；
    - ``"user_initiated"``：用户主动发起对话（非回复主动消息）→ 两层同幅小升（被需要感）。

    被接住/被冷落的判定与防重复结算由 ProactiveScheduler 负责：每条主动开口在
    ``_sent_records`` 里只有一条记录，承接即标 ``consumed``、超窗出队时按
    ``delivered`` 分流（未送达的不罚），因此天然不会重复计数、也不会罚到
    压根没发出去的开口。

    ❗ 沉默螺旋修复（v0.2.1）：``user_initiated`` **同时抬 branch 层**——他主动来找你
    就是「不排斥你」的最强信号，修复前只抬 self 层，导致branch 层触底后无论用户
    多主动都回不来。branch 层的长期回温由引擎 tick 的 ``_regress_branch_urge`` 负责
    （本函数只管事件驱动的即时升降）。
    """
    cfg = engine._plugin.config
    if not cfg.plugin.enabled or not cfg.narrative.enabled:
        return
    pro = cfg.proactive
    if not pro.urge_enabled:
        return

    gain = float(pro.urge_gain)
    decay = float(pro.urge_decay)

    # self 层：state["state"]["urge"]，clamp [0.05, 1.0]
    state = engine.load_self_state()
    inner = state["state"]
    urge = float(inner.get("urge", float(pro.urge_base)))
    if event == "caught":
        urge += gain
    elif event == "ignored":
        urge -= decay
    elif event == "user_initiated":
        urge += gain * 0.5
    else:
        # 未知事件立即暴露（项目 debug 规范：不兜底掩盖调用方笔误）
        raise ValueError(f"未知的分享欲事件类型: {event!r}")
    inner["urge"] = round(max(0.05, min(1.0, urge)), 3)
    engine.save_self_state(state)

    # branch 层：对特定对象的分享欲系数，clamp [urge_branch_floor, 1.0]。
    # 修复前 user_initiated 不进本分支（注释写「只说明被需要，不改对人系数」）——
    # 那是沉默螺旋的成因之一：唯一由用户掌控的正向信号救不了触底的对人系数。
    branch = engine.load_branch_state(user_id)
    factor = float(branch["state"].get("urge_factor", 1.0))
    if event == "caught":
        # 与 self 层同比例（gain : decay = 1 : 2.5）；修复前是 gain*0.5，奖惩比 5:1。
        factor += gain
    elif event == "ignored":
        factor -= decay
    else:  # user_initiated（未知事件已在上方 self 层分支抛错）
        factor += gain * 0.5
    branch["state"]["urge_factor"] = round(
        max(float(pro.urge_branch_floor), min(1.0, factor)), 3
    )
    engine.save_branch_state(user_id, branch)


def compute_share_urge(engine: Any, user_id: str) -> float:
    """合成当前分享欲 ∈ [0,1]：self 层 × branch 层 × 精力因子（相乘）。

    - self 层（``state["state"]["urge"]``）：基线漂移 + 事件升降，tick 回归维护；
    - branch 层（``branch["state"]["urge_factor"]``）：对该用户的亲近系数，缺省 1.0 中性；
    - 精力因子：精力低于基线时按比例拖累（累了不想说话），下限 0.4。

    只作用于主动开口的时机采样；**回复路径绝不调用本方法**——用户主动来找时
    bot 永不设门（访谈启发⑦ Agency Window 边界）。
    开关任一关闭（plugin / narrative / proactive.urge_enabled）→ 返回 1.0，
    即旧行为（到点必发）。
    """
    cfg = engine._plugin.config
    if not cfg.plugin.enabled or not cfg.narrative.enabled:
        return 1.0
    pro = cfg.proactive
    if not pro.urge_enabled:
        return 1.0

    state = engine.load_self_state()
    inner = state["state"]
    self_urge = float(inner.get("urge", float(pro.urge_base)))

    branch = engine.load_branch_state(user_id)
    branch_factor = float(branch["state"].get("urge_factor", 1.0))

    energy = float(inner["mood"].get("energy", 0.55))
    energy_baseline = max(float(cfg.narrative.energy_baseline), 0.05)
    energy_factor = max(0.4, min(1.0, energy / energy_baseline))

    urge = self_urge * branch_factor * energy_factor
    return round(max(0.0, min(1.0, urge)), 3)


__all__ = [
    "build_bysource",
    "compute_share_urge",
    "overlap_ratio",
    "record_urge_feedback",
]
