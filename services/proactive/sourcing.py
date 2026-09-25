"""由头取材（sourcing）：主动消息"说什么"的来源，以及分享欲的奖惩。

从 ``state/engine.py`` 拆出（v0.2.0 批 1，执行路线「类拆分推迟表」）：
``build_bysource`` / ``_bysource_used_key`` / ``compute_share_urge`` / ``record_urge_feedback``。
归到 proactive 包是因为它们只服务**主动开口**，与"世界推进"无关——engine 已经
900+ 行，主动链路的逻辑不该再往里堆。

依赖方向：本模块接受 ``engine`` 实例（不持有状态），engine 侧保留一层薄委托方法，
对外 API 不变（``engine.build_bysource`` 等照旧）。

⚠ 循环导入：``state/engine.py`` 会在模块顶层 import 本模块，故本模块**不得**在顶层
import engine。``INJECT_TEXT_CAP`` 采用函数内延迟导入（见 ``build_bysource``）。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..render.audience import filter_entries

# 由头去复用：已用作由头的生活片段 ts 集合（逗号分隔，有界 8 条）
# 按 user_id 命名空间隔离——store.get_kv_str 没有 scope 参数，而生活片段挂在
# 自我层（全局共享）：若所有用户共用一个键，先触发的用户会把素材用尽，
# 后触发的用户取不到由头 → build_bysource 返回空 → 本轮主动开口被跳过。
# 同一件事讲给不同朋友听是自然的，所以去复用只约束同一段关系。
_BYSOURCE_USED_KEY_PREFIX = "bysource:used:"

#: 由头与最近对话的文本重叠上限（OBSERVE(R23)）：超过则判定"撞车"，换一个候选。
#: 中文无词边界，用**字符 bigram 的 Jaccard 相似度**——零依赖、离线可重算。
#: 取值先拍一个保守值，真机跑一轮后按回放台数据调。
_OVERLAP_REJECT = 0.5


def _bysource_used_key(user_id: str) -> str:
    """某用户已用作由头的片段 ts 集合的 kv 键（按 uid 隔离）。"""
    return f"{_BYSOURCE_USED_KEY_PREFIX}{user_id}"


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
    1. 创作层最近生活片段（focus.pending_events 最新两条）；
    2. 支线里程碑（你们之间发生过的事）；
    3. 情绪/作息（疲惫想倾诉、深夜清醒）。

    **tier 只定详略，不定优先级**（v0.2.0 批 1 / R22）：真正的取材优先级是
    ① 未用过（同一段关系内去复用）+ ② 与最近对话不撞车（文本重叠，R23）。
    minor 档的质量门槛保留——内容太薄，发出去大概率没人接。

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

    # (候选文本, 片段 ts)；非片段来源 ts 为空串，用于选中后登记"已用"
    candidates: List[Tuple[str, str]] = []
    # 受众过滤（ADR-0004）：生活片段默认通用（无标签），但被打标就必须按受众隔离
    pending = filter_entries(state["state"]["focus"].get("pending_events", []), user_id)
    for item in pending[-2:]:
        fragment = str(item.get("text", "") or "").strip()
        if not fragment:
            continue
        ts = str(item.get("ts", "") or "").strip()
        # 去复用：同一片段不作二次由头
        if ts and ts in used:
            continue
        # 质量门槛：minor 档（无素材）不单独作由头
        if str(item.get("tier", "") or "").strip() == "minor":
            continue
        # 与最近对话撞车则跳过（OBSERVE(R23)）：由头要是"新事"，不是刚聊过的复述
        if recent_text and overlap_ratio(fragment, recent_text) >= _OVERLAP_REJECT:
            continue
        candidates.append((f"最近一段生活：{fragment[:INJECT_TEXT_CAP]}", ts))

    stage = str(branch["identity"].get("stage", "陌生人"))
    milestones = list(branch["state"].get("milestones", []))
    if milestones and stage != "陌生人":
        latest_milestone = milestones[-1]
        candidates.append((f"想起我们之间那件事：{latest_milestone.get('desc', '')}", ""))

    if not candidates:
        mood = str(state["state"]["mood"].get("label", "平静"))
        if mood in ("低落", "疲惫"):
            candidates.append((f"今天有点{mood}，想找人聊聊", ""))
        routine = state["state"].get("routine", {})
        phase = str(routine.get("phase", ""))
        # 睡着就别说「还不想睡」——睡眠态上线后这条只在清醒的深夜才成立
        # （实际上深夜既在静默期又在睡眠窗口内，本分支基本不可达，留着只为
        #  日后把静默期调窄时语义仍然正确）
        if phase == "深夜" and str(routine.get("sleep_state", "awake")) != "asleep":
            candidates.append(("夜深了，我还不想睡，想跟你说点什么", ""))
        elif phase == "清晨":
            candidates.append(("刚醒，今天莫名的想先跟你说句话", ""))

    if not candidates:
        return ""  # 无可借由的生活素材：本轮主动取消（宁可缺席，不干聊）

    # 场景感补全：确定性选一个（按小时稳定），避免同一天重复同一由头
    seed = sum(ord(char) for char in user_id) + current.hour + (current.date().day * 7)
    chosen, chosen_ts = candidates[seed % len(candidates)]
    if chosen_ts:
        # 登记已用（有界 8 条）：下次该片段不再作由头（仅对该用户生效）
        used.add(chosen_ts)
        engine._store.set_kv_str(_bysource_used_key(user_id), ",".join(sorted(used)[-8:]))
    return chosen


# ─── 分享欲 share_urge（v0.1.8 第一步：动机驱动主动时机） ──────


def record_urge_feedback(engine: Any, user_id: str, event: str) -> None:
    """分享欲事件反馈（规则层零 LLM，第一步方案）。

    event 取值：
    - ``"caught"``：主动消息在承接窗口（16h）内被接住 → self/branch 双升（聊得起来，更想聊）；
    - ``"ignored"``：主动消息**已确认送达**但超窗无人接住 → self/branch 双降（别热脸贴冷屁股）；
    - ``"user_initiated"``：用户主动发起对话（非回复主动消息）→ 仅 self 层小升（被需要感）。

    被接住/被冷落的判定与防重复结算由 ProactiveScheduler 负责：每条主动开口在
    ``_sent_records`` 里只有一条记录，承接即标 ``consumed``、超窗出队时按
    ``delivered`` 分流（未送达的不罚），因此天然不会重复计数、也不会罚到
    压根没发出去的开口。
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

    # branch 层：对特定对象的分享欲系数，clamp [urge_branch_floor, 1.0]；
    # user_initiated 只说明"被需要"，不改对人系数。
    if event in ("caught", "ignored"):
        branch = engine.load_branch_state(user_id)
        factor = float(branch["state"].get("urge_factor", 1.0))
        if event == "caught":
            factor += gain * 0.5
        else:
            factor -= decay
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
