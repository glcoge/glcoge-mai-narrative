"""三层连续性 —— 受控白名单 schema（批 2）+ 晋升状态机（批 4）。

本模块是 ADR-0002 的**声明层**：三层各自的字段白名单、锚定层护栏常量、
世界规则守卫的关键词抽取与判定。批 2 只落「声明 + 守卫」，不落晋升逻辑。

批 4 在此追加：晋升状态机（门槛/冷却/反证/证据纪律五条）+ 留痕 + 回滚。

设计纪律
--------
- **单一事实源**：白名单常量只在本模块定义，别处一律 import，不得就地复制。
- **fail-closed**：未登记字段默认不可见（``is_slow_field_visible``），新增维度
  必须显式登记，防"忘了登记 = 静默泄露/静默污染"。
- 本模块**不 import** engine/creator 等上层模块，保持零依赖（避免循环导入）。
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

# ─── 慢变区白名单（ADR-0002 §1） ──────────────────────────────────
#: 受控维度：LLM 只能在这些路径上提案，**不得自由开字段**。
#: 值为允许的叶子路径（点分），与 ADR-0002 §1 一一对应。
SLOW_FIELD_PATHS: FrozenSet[str] = frozenset(
    {
        "perspective.world_view",
        "perspective.life_goals",
        "relationship.trust",
        "relationship.closeness",
        "relationship.boundaries",
        "relationship.stage",
    }
)

#: 慢变区各维度的受众规则：general = 可进通用注入；per_user = 只对该用户可见。
#: 未登记维度一律 fail-closed（不可见），见 ``is_slow_field_visible``。
SLOW_FIELD_AUDIENCE: Dict[str, str] = {
    "perspective.world_view": "general",
    "perspective.life_goals": "general",
    "relationship.trust": "per_user",
    "relationship.closeness": "per_user",
    "relationship.boundaries": "per_user",
    "relationship.stage": "per_user",
}

#: 关系四维（ADR-0002 §1）。``stage`` 由前三者 + 证据晋升产生，**可回退**。
RELATIONSHIP_DIMENSIONS: Tuple[str, ...] = ("trust", "closeness", "boundaries", "stage")
#: 关系四维里由证据晋升产生的维度（前三者是原始观测，stage 是派生结论）。
RELATIONSHIP_DERIVED = "stage"

#: 关系**只读事实**字段（ADR-0002 §2）：是记录不是演化观点，不参与晋升。
RELATIONSHIP_FACT_FIELDS: Tuple[str, ...] = ("first_met", "milestones")

#: 自我层「看法」维度（ADR-0002 §1，批 4-C1）：**general 受众**——可进通用注入，
#: 也是 ``[learned]`` 投影的唯一来源（relationship 是 per_user，永不进 config.toml）。
PERSPECTIVE_FIELDS: Tuple[str, ...] = ("world_view", "life_goals")

#: 慢变写入者登记（批 4-C1）：写入必须**具名**。未登记 actor 一律拒绝——
#: 「谁把她的看法改了」是事后可审计的前提，匿名写入等于把留痕链断在源头。
SLOW_WRITE_ACTORS: FrozenSet[str] = frozenset({"seed", "promotion", "rollback", "manual"})

#: 明确排除的人格维度（ADR-0002 §1）：锚定层人格归宿主 `[personality]`，
#: 让 LLM 学 traits 等于把双人格事故从后门放回来。
SLOW_FIELD_EXCLUDED_PREFIXES: Tuple[str, ...] = ("character.", "traits", "personality")

# ─── 锚定层（ADR-0002 §9） ────────────────────────────────────────
#: 锚定层字段：**永不改变**。命名空间前缀形式，供写入路径显式排除。
#: 锚定层真源是宿主 `[personality]` + 插件 config `[identity]`，不在运行时 state 内；
#: 本常量用于「写入路径不得触碰锚定维度」的可判定断言。
ANCHOR_FIELDS: FrozenSet[str] = frozenset(
    {
        "identity.world",
        "identity.values",
        "identity.world_rules",
        "personality",
    }
)

#: 锚定层字段名的**前缀**黑名单：任何写入路径只要路径以此开头即拒绝。
#: 与 ``ANCHOR_FIELDS`` 的差别：这里管的是"以锚定层为根"的整棵子树。
ANCHOR_ROOT_PREFIXES: Tuple[str, ...] = ("identity.", "personality", "character.")


def is_anchor_field(path: str) -> bool:
    """判断某个字段路径是否属于锚定层（写入路径必须据此排除）。"""
    normalized = str(path or "").strip()
    if not normalized:
        return False
    if normalized in ANCHOR_FIELDS:
        return True
    return any(normalized.startswith(prefix) for prefix in ANCHOR_ROOT_PREFIXES)


def is_slow_field(path: str) -> bool:
    """判断某个字段路径是否在白名单内（白名单外一律拒绝提案）。"""
    return str(path or "").strip() in SLOW_FIELD_PATHS


def assert_writable(path: str) -> str:
    """写入路径守卫：锚定层字段**永不**可写，命中即抛 ``PermissionError``。

    任何写入慢变/漂移状态的路径（批 3 漂移调制、批 4 晋升写回）都必须在落盘前
    调用本函数。批 2 先把它立在声明层，批 3/4 的写入路径接入即被同一个守卫覆盖
    ——这是「锚定层永不被写」从承诺变成可执行约束的唯一办法（ADR-0002 §9）。

    返回原路径，便于 ``state[assert_writable(p)] = v`` 风格调用。
    """
    normalized = str(path or "").strip()
    if is_anchor_field(normalized):
        raise PermissionError(
            f"锚定层字段不可写：{normalized!r}（ADR-0002 §9：锚定层永不改变）"
        )
    return normalized


def is_slow_field_visible(path: str, audience: str, owner_uid: str = "") -> bool:
    """慢变字段对当前受众是否可见（未登记维度 fail-closed）。

    Args:
        path: 慢变字段路径（如 ``relationship.trust``）。
        audience: 当前受众（uid 或空）。
        owner_uid: 字段归属者（关系类维度按 uid 隔离时使用）。
    """
    rule = SLOW_FIELD_AUDIENCE.get(str(path or "").strip())
    if rule is None:
        return False  # fail-closed：没登记的维度不猜
    if rule == "general":
        return True
    target = str(audience or "").strip()
    if not target:
        return False
    owner = str(owner_uid or "").strip()
    return target == owner if owner else True


def current_relationship_stage(facts: Dict[str, Any]) -> str:
    """从**只读事实**确定性推导关系阶段标签（批 2 空窗期过渡实现）。

    背景（批 2 结构性约束）：ADR-0002 §1 规定 ``stage`` 由 trust/closeness/
    boundaries 三维 + 证据**晋升**产生，但晋升机是批 4。批 2 删掉旧的
    familiarity 规则线后到批 4 之间没有任何东西写关系四维 → 若 stage 直接留空，
    注入块连「关系」行都没有，聊天体感明显变空。

    故此处用**只读事实**（milestones / first_met）做确定性推导：不依赖被删的
    familiarity，不引入随机（保离线回放确定性）。批 4 晋升机上线时把本函数换成
    「可回退的晋升值」，切换点干净。

    规则（确定性、只进不退的**事实**驱动）：
    - 无 facts / 无 milestones → 陌生人
    - 有里程碑但无 stage 类里程碑 → 相识
    - 里程碑里有 ``stage:{label}`` → 取最后一次晋升到的 label
    """
    if not isinstance(facts, dict):
        return "陌生人"
    milestones = facts.get("milestones") or []
    if not isinstance(milestones, list) or not milestones:
        return "陌生人"
    latest_stage = ""
    for item in milestones:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "")
        if mid.startswith("stage:"):
            latest_stage = str(item.get("stage_label") or mid[len("stage:") :]).strip()
    if latest_stage:
        return latest_stage
    return "相识"


def normalize_relationship(state: Dict[str, Any]) -> Dict[str, Any]:
    """把支线 state 的关系部分规范到四维 schema（缺维度补默认值）。

    只补不删：保留 ``first_met`` / ``milestones`` 等只读事实字段。
    """
    relationship = state.setdefault("relationship", {})
    for dimension in RELATIONSHIP_DIMENSIONS:
        relationship.setdefault(dimension, 0.0 if dimension != RELATIONSHIP_DERIVED else "陌生人")
    for fact in RELATIONSHIP_FACT_FIELDS:
        relationship.setdefault(fact, "" if fact == "first_met" else [])
    return relationship


def normalize_perspective(state: Dict[str, Any]) -> Dict[str, Any]:
    """把自我层 state 的 ``perspective`` 部分规范到 schema（只补不删）。

    与 ``normalize_relationship`` 对称。字段语义：``world_view`` 是文本、
    ``life_goals`` 是字符串列表、``origin`` 记录来源（seed/promotion/manual）、
    ``updated_ts`` 记录最近写入时刻——后两者是**溯源**字段，晋升留痕靠它们。

    ``perspective`` 存在但类型不对（被脏数据写成字符串等）时**整段重建**：
    类型错误的容器无法"只补不删"，硬补会留下混合形状，比重建更难排查。
    """
    perspective = state.get("perspective")
    if not isinstance(perspective, dict):
        perspective = {}
        state["perspective"] = perspective
    perspective.setdefault("world_view", "")
    perspective.setdefault("life_goals", [])
    perspective.setdefault("origin", "")
    perspective.setdefault("updated_ts", "")
    if not isinstance(perspective.get("life_goals"), list):
        perspective["life_goals"] = []
    return perspective


def _split_slow_path(path: str) -> Tuple[str, str]:
    """拆「顶层段.叶子字段」。本项目的慢变路径恒为两段（ADR-0002 §1 白名单形状）。"""
    normalized = str(path or "").strip()
    head, _, tail = normalized.partition(".")
    if not head or not tail or "." in tail:
        raise ValueError(f"慢变路径必须形如 <段>.<字段>：{normalized!r}")
    return head, tail


def slow_get(state: Dict[str, Any], path: str) -> Any:
    """读慢变现值（**白名单外一律抛 ValueError**，不静默返回 None）。

    静默返回 None 会让「路径写错」伪装成「值还没写」，是批 3 教训「功能静默失效」
    的同一类坑；这里宁可炸。
    """
    if not is_slow_field(path):
        raise ValueError(f"非慢变白名单路径：{path!r}（ADR-0002 §1：受控维度，不得自由读取）")
    head, tail = _split_slow_path(path)
    section = state.get(head)
    if not isinstance(section, dict):
        return None
    return section.get(tail)


def slow_set(state: Dict[str, Any], path: str, value: Any, *, actor: str) -> str:
    """写慢变现值。三个拒绝分支**互不掩盖**，顺序即优先级：

    1. 锚定层路径 → ``PermissionError``（ADR-0002 §9，语义最强，先判）
    2. 非白名单路径 → ``ValueError``（不得自由开字段）
    3. 未登记 actor → ``ValueError``（写入必须具名，留痕链的起点）

    返回原路径，便于 ``state[slow_set(...)] = v`` 风格调用。
    """
    assert_writable(path)
    if not is_slow_field(path):
        raise ValueError(f"非慢变白名单路径：{path!r}（不得自由开字段）")
    if actor not in SLOW_WRITE_ACTORS:
        raise ValueError(f"未登记的慢变写入者：{actor!r}（可选 {sorted(SLOW_WRITE_ACTORS)}）")
    head, tail = _split_slow_path(path)
    section = state.get(head)
    if not isinstance(section, dict):
        section = {}
        state[head] = section
    section[tail] = value
    return path


#: 由「文本形态」还原的慢变维度（LLM 提案与审计表里，值恒为字符串）。
#: ⚠️ ``stage`` **不在**浮点集合里——它是阶段标签（"亲近"），不是数值。
_LIST_LEAVES: FrozenSet[str] = frozenset({"life_goals"})
_FLOAT_LEAVES: FrozenSet[str] = frozenset({"trust", "closeness", "boundaries"})


def coerce_slow_value(path: str, value: Any) -> Any:
    """把慢变值还原成该维度的**真实类型**（批 4 实现期发现，rollback 与 apply 共用）。

    提案通道与 ``promotions`` 审计表里的值**恒为字符串**（``proposed_value`` 的契约
    是非空文本），但慢变维度里 ``life_goals`` 是列表、``trust``/``closeness`` 是浮点。
    不还原就直接写，是**静默数据损坏**：列表被写成字符串（下一轮 ``normalize_*``
    再把它重置成空）、浮点被写成字符串（下游 ``float()`` 才发现）。

    - ``life_goals``：已是列表则原样；字符串先试 JSON，再按行/分号切分；都不成 → 单项列表
    - ``trust``/``closeness``/``boundaries``/``stage``：转浮点（失败 → 0.0）
    - 其余（``world_view``）：转字符串
    """
    leaf = str(path or "").rsplit(".", 1)[-1]
    if leaf in _LIST_LEAVES:
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        text = str(value if value is not None else "").strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        parts = [
            piece.strip()
            for line in text.splitlines()
            for piece in line.replace("；", ";").split(";")
        ]
        cleaned = [piece for piece in parts if piece]
        return cleaned or [text]
    if leaf in _FLOAT_LEAVES:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    return str(value if value is not None else "")


def guard_keywords(world_rules: Iterable[str], values: Iterable[str], extra: Iterable[str]) -> Set[str]:
    """汇总守卫关键词（ADR-0002 §9 双向守卫）。

    来源三路（E6 裁决：自动抽取 + 手工补充）：
    1. ``[identity].world_rules`` 自动抽取——规则原文里"不可违背"的名词性片段
    2. ``[identity].values`` 自动抽取——价值观底线词
    3. ``[anchor].guard_keywords`` 手工补充（默认空）

    中文无分词，故自动抽取采用**保守切分**：按标点/空格切成子句，取 2~8 字的
    子句整体作关键词，避免逐字切分造成的误伤（纯自动分词的已知缺陷）。
    """
    keywords: Set[str] = set()
    for text in list(world_rules or []) + list(values or []):
        keywords.update(_keyword_fragments(str(text or "")))
    for text in extra or []:
        fragment = str(text or "").strip()
        if fragment:
            keywords.add(fragment)
    return keywords


#: 关键词切分的分隔符（中英文标点 + 空白）。**不是**分词，是切子句。
_KEYWORD_SEPARATORS = "，。；！？、,.;!?（）()【】[]「」“”\"' \t\n—－-"
#: 关键词长度上下限：过短易误伤（"的"），过长不可能命中。
_KEYWORD_MIN_LEN = 2
_KEYWORD_MAX_LEN = 8

#: 中文**语气/功能前缀**：真实规则几乎都写成祈使句（"不可说谎""不得泄露""不要迟到"），
#: 整句作关键词永远不可能命中产出文本（产出不会复述规则原文）。故剥掉前缀留下真正的
#: **内容词根**——这是"从规则原文抽可命中片段"与"不误伤正常表达"之间的唯一可行折中
#: （中文无分词）。
#:
#: ⚠️ 这里只收**纯语气助词**（"不可/不得/不要/禁止/…"），不收实义动词组合：
#: "不可说谎" → "说谎"（可命中）；若把"不可泄露"整段当前缀，会剥出"内部代号"这种
#: 只剩名词根的碎片，反而把"泄露"这个真正的行为词丢掉。
_KEYWORD_STRIP_PREFIXES = (
    "不可", "不得", "不要", "不能", "不会", "禁止", "严禁", "防止", "避免", "拒绝",
    "绝不", "勿", "别",
)
#: 剥前缀后的最小长度。取 2 是刻意的：`不可说谎` → `说谎` 正好 2 字，而这是最常见
#: 的规则形态；若取下限 3，最自然的规则写法一律抽不出可命中关键词 → 守卫形同虚设。
#: 代价是 2 字碎片可能误伤（如"说谎"撞到"别对我撒谎"不会命中，但撞到"说谎者"会）——
#: 这属于保守放行的可接受代价：漏网的还有注入侧第二道闸，误杀则直接吞掉正常内容。
_KEYWORD_STRIP_MIN_LEN = 2


def build_guard_keywords(config: Any) -> Set[str]:
    """从插件配置装配守卫关键词集合（ADR-0002 §9 双向守卫的**唯一入口**）。

    配置段解耦（缺段不炸）：老配置 / 测试假件没有 ``[anchor]`` 或 ``[identity]``
    的 ``guard_fragments`` 时按空处理——**不是**用 getattr 掩盖错误，而是这三项
    来源本就都是"可选补充"，缺一个就少一路。
    """
    identity = getattr(config, "identity", None)
    anchor = getattr(config, "anchor", None)
    world_rules = list(getattr(identity, "world_rules", None) or [])
    values = list(getattr(identity, "values", None) or [])
    extra = list(getattr(identity, "guard_fragments", None) or [])
    extra += list(getattr(anchor, "guard_keywords", None) or [])
    return guard_keywords(world_rules, values, extra)


def guard_violations(text: str, keywords: Set[str]) -> List[str]:
    """返回 ``text`` 命中的关键词（**排序后**，便于日志与断言稳定复现）。

    零误判优先：只在整词出现时命中，不做模糊匹配（中文模糊匹配的误杀率
    远高于漏网率，而漏网还有注入侧第二道闸兜着）。
    """
    normalized = str(text or "")
    if not normalized or not keywords:
        return []
    return sorted(str(word) for word in keywords if str(word) in normalized)


def guard_blocks_entry(entry: Dict[str, Any], keywords: Set[str]) -> bool:
    """条目是否命中守卫（只看 ``text`` 字段）。

    ``text`` 非字符串/缺失时返回 False —— 结构异常交给别的层暴露，
    守卫不做结构校验（那会让一个格式 bug 伪装成"内容违规"）。
    """
    if not isinstance(entry, dict):
        return False
    return bool(guard_violations(entry.get("text"), keywords))


def should_drop_output(text: str, keywords: Set[str]) -> bool:
    """第一道闸（**入库前**）：创作产出是否要被丢弃。

    空产出返回 False —— "没生成"不是"违禁"，由调用方原样 return 即可，
    不该让守卫把它算成一次拦截（否则计数指标会被空产出污染）。

    命中后的动作是**丢弃整条产出**，不是改写：改写等于让代码替模型撒谎，
    且改写后的文本没有经过任何审查，只是把违规面藏得更深（E7 裁决）。
    """
    return bool(guard_violations(text, keywords))


def filter_guarded_entries(
    entries: Iterable[Dict[str, Any]], keywords: Set[str]
) -> List[Dict[str, Any]]:
    """第二道闸（**注入前**）：逐条过滤，只丢命中的条目（不丢整段）。

    返回**新列表**（不改原序列：同一条entry 序列可能同时被受众过滤消费，
    就地改会串味）。全被拦时返回空列表，调用方据此跳过该行不渲染空壳。

    为什么是"丢条目"而非"丢整段"：注入段同时承载状态/关系/由头，
    因为一条素材违禁就把整段丢掉，会让这一轮的注入直接变空（E7 裁决）。
    """
    return [entry for entry in entries if not guard_blocks_entry(entry, keywords)]


def _keyword_fragments(text: str) -> List[str]:
    """把一条规则/价值观原文切成候选关键词（保守、可预期）。

    返回可能**同时**包含原文子句与剥前缀后的内容词根——保守起见两条都留着：
    用户可能真写"我以诚实守信为底线"（整句就是内容），也可能写"不可说谎"
    （整句永远不可能命中，只有"说谎"有用）。
    """
    fragments: List[str] = []
    current = ""
    for char in text:
        if char in _KEYWORD_SEPARATORS:
            if current:
                fragments.append(current)
            current = ""
        else:
            current += char
    if current:
        fragments.append(current)

    candidates: List[str] = []
    for fragment in fragments:
        candidates.append(fragment)
        stripped = _strip_imperative_prefix(fragment)
        if stripped and stripped != fragment:
            candidates.append(stripped)
    return [
        candidate
        for candidate in candidates
        if _KEYWORD_MIN_LEN <= len(candidate) <= _KEYWORD_MAX_LEN
    ]


def _strip_imperative_prefix(fragment: str) -> str:
    """剥掉祈使句前缀（"不可说谎" → "说谎"）；不合条件时原样返回。

    剥完若短于 ``_KEYWORD_STRIP_MIN_LEN`` 就不剥（"不可说" → "说" 只剩 1 字，
    误伤面远大于收益）。
    """
    for prefix in _KEYWORD_STRIP_PREFIXES:
        if fragment.startswith(prefix):
            remainder = fragment[len(prefix) :]
            if len(remainder) >= _KEYWORD_STRIP_MIN_LEN:
                return remainder
            return fragment
    return fragment


# ─── 晋升状态机（批 4-C4：确定性代码，零 LLM） ────────────────────

#: 关系的**自动**晋升目标维度。
#: **刻意不含 ``boundaries``**（用户 2026-09-26 强约束 2）：边界感的移动需要读懂
#: 「抗议 / 舒适」的交互质量，计数推不出来——宁可停在锚定值，不许计数瞎推。
#: 人工仍可写（``slow_set(..., actor="manual")``）。
RELATIONSHIP_AUTO_PROMOTE: Tuple[str, ...] = ("trust", "closeness")

#: 晋升审计动作类型（与 ``store.promotions.action`` 对应）。
PROMOTION_ACTIONS: Tuple[str, ...] = ("applied", "rejected", "rolled_back")

#: 关系阶段分档（E13：``stage`` 是**派生结论**，由 trust+closeness 确定性推导）。
#: 按 (下限, 标签) 降序取两维**均值**。**只读两维**——boundaries 被冻结，
#: 把它算进来会让阶段卡死在锚定值上。
_STAGE_BANDS: Tuple[Tuple[float, str], ...] = (
    (0.75, "很亲近"),
    (0.5, "亲近"),
    (0.25, "相识"),
    (0.0, "陌生人"),
)

#: 短声明下限：``proposed_value`` 短于此长度不参与合并（HDSI 3.3「短声明误并」）。
MERGE_MIN_VALUE_LEN = 8


def _row_field(row: Any, key: str) -> str:
    """宽松取字段（dict / sqlite3.Row 皆可），统一转字符串。"""
    try:
        value = row.get(key) if hasattr(row, "get") else row[key]
    except (TypeError, KeyError, IndexError):
        return ""
    return str(value or "").strip()


def audit_text(value: Any) -> str:
    """把慢变值编码成**可逆**的审计文本（列表走 JSON，其余走 str）。

    ``promotions`` 表的 old/new 值是回滚的依据——列表若用 ``str()`` 落库会变成
    ``"['学会游泳']"``（单引号，不是合法 JSON），回滚时就解析不回来。
    """
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value if value is not None else "")


def parse_id_list(text: Any) -> List[int]:
    """解析逗号分隔的 id 串（``contradicts`` 用）；非法项静默跳过。"""
    result: List[int] = []
    for chunk in str(text or "").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            result.append(int(chunk))
        except ValueError:
            continue
    return result


def parse_chronicle_refs(text: Any) -> List[int]:
    """解析 ``chronicle:<id>`` 引用串（逗号分隔）为条目 id 列表。"""
    result: List[int] = []
    for chunk in str(text or "").split(","):
        token = chunk.strip()
        if not token:
            continue
        if ":" in token:
            token = token.split(":", 1)[1].strip()
        try:
            result.append(int(token))
        except ValueError:
            continue
    return result


def scene_stats(rows: Iterable[Any]) -> Tuple[int, int]:
    """从证据条目算 ``(场景数, 跨日数)``。

    场景键 = ``(自然日, 来源)``——**同一天同一来源只算 1 个场景**。真实归档数据里
    去重比最高 18.56（167 条事件压成 9 天），不去重则一天即可刷满门槛。
    """
    scenes: Set[Tuple[str, str]] = set()
    days: Set[str] = set()
    for row in rows:
        day = _row_field(row, "ts")[:10]
        if len(day) != 10:
            continue
        scenes.add((day, _row_field(row, "source_uid")))
        days.add(day)
    return len(scenes), len(days)


def meets_minor_gate(
    *,
    confidence: float,
    scene_count: int,
    day_count: int,
    min_confidence: float,
    min_scenes: int,
    min_days: int,
) -> bool:
    """minor 门槛：置信 AND 场景 AND 跨日，三条**全过**才算（P3）。"""
    return (
        float(confidence) >= float(min_confidence)
        and int(scene_count) >= int(min_scenes)
        and int(day_count) >= int(min_days)
    )


def meets_major_gate(
    *, confidence: float, scene_count: int, min_confidence: float, min_scenes: int
) -> bool:
    """major 门槛：置信 AND 场景（**不限天数**，P4；默认关）。"""
    return float(confidence) >= float(min_confidence) and int(scene_count) >= int(min_scenes)


def is_after_cooldown(
    last_ts: str, *, now: datetime.datetime, cooldown_hours: float
) -> bool:
    """距上次晋升是否已过冷却（读不到 / 解析失败 = 从未晋升 → 不在冷却中）。"""
    if not str(last_ts or "").strip():
        return True
    try:
        last = datetime.datetime.fromisoformat(str(last_ts).strip())
    except ValueError:
        return True
    return (now - last).total_seconds() >= float(cooldown_hours) * 3600


def apply_refutation_penalty(confidence: float, *, penalty: float) -> float:
    """反证扣减（P6）：``confidence − penalty``，下限 0。"""
    return max(0.0, float(confidence) - float(penalty))


def merge_confidence(
    current: float, existing: float, *, bonus: float, new_scene: bool
) -> float:
    """重复提案合并（P7）：``min(本次, 旧值 + bonus)``，**仅新独立场景才加**。

    复述不加信——HDSI 2.1 的事故形态就是「整理器反复叙述把推测固化成事实」。
    """
    if not new_scene:
        return float(current)
    return min(float(current), float(existing) + float(bonus))


def relation_value(
    scene_days: int,
    *,
    min_days: int,
    days_per_step: int,
    step: float,
    max_value: float,
) -> float:
    """关系维度的确定性取值曲线（E14）。

    达到起步门槛即得**第一档**；此后每 ``days_per_step`` 个场景日升一档，封顶
    ``max_value``（留余量给批 5 的 LLM 提案与人工——确定性计数不该把关系推顶格）。

    例（出厂值 3 / 2 / 0.05 / 0.8）：3 日→0.05，5 日→0.10，19 日→0.45，33 日→0.80。
    """
    days = int(scene_days or 0)
    floor = max(1, int(min_days))
    if days < floor:
        return 0.0
    per_step = max(1, int(days_per_step))
    steps = (days - floor) // per_step + 1
    return min(float(max_value), round(steps * float(step), 4))


def derive_stage(trust: float, closeness: float) -> str:
    """由 trust / closeness 推导关系阶段标签（E13：**只读两维**）。

    ``stage`` 是派生结论（``RELATIONSHIP_DERIVED``），由计数直接推「关系阶段」这种
    用户可见的总结论最容易跑偏；随 trust/closeness 派生则保守且自洽。
    """
    score = (float(trust or 0.0) + float(closeness or 0.0)) / 2.0
    for floor, label in _STAGE_BANDS:
        if score >= floor:
            return label
    return "陌生人"


class PromotionEngine:
    """晋升状态机（确定性，零 LLM）。

    依赖方向：只通过 ``plugin._engine`` / ``plugin._store`` 访问状态与存储，
    **不 import** engine/store 模块（保持本模块零依赖），也**不 import**
    ``learning.evidence``——关系晋升所需的正向场景日由**调用方算好后传入**，
    免得 ``evidence → continuity`` 的既有依赖变成循环。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    # ── 便捷访问 ────────────────────────────────────────────────

    @property
    def _store(self) -> Any:
        return self._plugin._store

    @staticmethod
    def _iso(now: datetime.datetime) -> str:
        return now.isoformat(timespec="seconds")

    def _count(self, kind: str) -> None:
        telemetry = getattr(self._plugin, "_telemetry", None)
        if telemetry is not None:
            telemetry.record_counter(kind)

    # ── 冷却（跨重启持久化） ────────────────────────────────────

    def _cooldown_key(self, path: str) -> str:
        return f"promotion:cooldown:{path}"

    def last_promoted_ts(self, path: str) -> str:
        """上次晋升时刻（历史遗留：函数名沿用「promoted」语义）。"""
        return self._store.get_kv_str(self._cooldown_key(path), "")

    def mark_cooldown(self, path: str, now: datetime.datetime) -> None:
        """记录晋升时刻（冷却以 kv 存 ISO 时间戳 → **跨重启**）。"""
        self._store.set_kv_str(self._cooldown_key(path), self._iso(now))

    def is_cooling(self, path: str, now: datetime.datetime) -> bool:
        config = self._plugin.config.promotion
        return not is_after_cooldown(
            self.last_promoted_ts(path),
            now=now,
            cooldown_hours=float(config.cooldown_hours or 0),
        )

    # ── 判定 ────────────────────────────────────────────────────

    def evaluate(self, proposal: Any, *, now: datetime.datetime) -> Dict[str, Any]:
        """判定一条提案是否够门槛（不写任何状态）。"""
        config = self._plugin.config.promotion
        path = _row_field(proposal, "path")
        confidence = float(_row_field(proposal, "confidence") or 0.0)
        rows = self._store.list_chronicle_by_ids(
            parse_chronicle_refs(_row_field(proposal, "evidence_refs"))
        )
        scene_count, day_count = scene_stats(rows)
        result: Dict[str, Any] = {
            "promote": False,
            "reason": "",
            "confidence": confidence,
            "scene_count": scene_count,
            "day_count": day_count,
        }
        if self.is_cooling(path, now):
            result["reason"] = "cooldown"
            return result
        if meets_minor_gate(
            confidence=confidence,
            scene_count=scene_count,
            day_count=day_count,
            min_confidence=float(config.minor_confidence),
            min_scenes=int(config.minor_min_scenes),
            min_days=int(config.minor_min_days),
        ):
            result["promote"] = True
            result["reason"] = "promote_minor"
            return result
        if bool(config.major_enabled) and meets_major_gate(
            confidence=confidence,
            scene_count=scene_count,
            min_confidence=float(config.major_confidence),
            min_scenes=int(config.major_min_scenes),
        ):
            result["promote"] = True
            result["reason"] = "promote_major"
            return result
        result["reason"] = "below_gate"
        return result

    # ── 执行（perspective） ─────────────────────────────────────

    def apply(self, proposal: Any, *, now: datetime.datetime) -> Dict[str, Any]:
        """判定 + 执行 + 审计 + 留痕 + 计数器；不够门槛则原样返回判定。

        写现值走 ``slow_set(..., actor="promotion")`` —— 锚定层守卫与白名单校验
        在写之前自动生效（ADR-0002 §9）。
        """
        decision = self.evaluate(proposal, now=now)
        if not decision["promote"]:
            return dict(decision, applied=False)

        path = _row_field(proposal, "path")
        # 提案契约里的值恒为字符串；慢变维度里 life_goals 是列表、trust 是浮点 →
        # 先还原真实类型再写，否则是静默数据损坏（见 coerce_slow_value）。
        new_value = coerce_slow_value(path, _row_field(proposal, "proposed_value"))
        engine = self._plugin._engine
        state = engine.load_self_state()
        normalize_perspective(state)
        old_value = slow_get(state, path)
        slow_set(state, path, new_value, actor="promotion")
        perspective = state["perspective"]
        perspective["origin"] = "promotion"
        perspective["updated_ts"] = self._iso(now)
        engine.save_self_state(state)

        proposal_id = proposal.get("id") if hasattr(proposal, "get") else None
        self._store.append_promotion(
            action="applied",
            target=_row_field(proposal, "target") or "perspective",
            path=path,
            old_value=audit_text(old_value),
            new_value=audit_text(new_value),
            reason=str(decision["reason"]),
            proposal_id=int(proposal_id) if proposal_id is not None else None,
            ts=self._iso(now),
        )
        if proposal_id is not None:
            self._store.update_proposal_status(int(proposal_id), "applied")
        self.mark_cooldown(path, now)
        # 人可读留痕。⚠️ kind=promotion 本身**被证据白名单拒之门外**（C2）：
        # 留痕是关于她学习状态的模板输出，拿它当晋升证据 = 自我强化循环。
        self._store.append_chronicle(
            "self",
            "promotion",
            f"看法更新（{decision['reason']}）：{path} ← {audit_text(new_value)}",
            ts=self._iso(now),
        )
        self._count("promotions")
        return dict(
            decision, applied=True, old_value=audit_text(old_value), new_value=new_value
        )

    # ── 反证 ────────────────────────────────────────────────────

    def apply_refutations(self, *, now: datetime.datetime) -> List[int]:
        """扫描待决提案：被**其它提案**引用为反证的 → 扣信 → 不足门槛即 rejected。

        匹配键是 **proposal id 显式引用**，不做文本相似度（HDSI 3.3 的字面归并器
        「短声明误并、长改写漏并」是作者知情未修的漏洞）。额外契约校验：反证必须
        指向**同路径**的提案，否则不生效。返回被驳回的提案 id 列表。

        ⚠️ 反证草稿本身**不留存**为新候选（HDSI 1.10：防结构化产物循环喂回模型）。
        """
        config = self._plugin.config.promotion
        penalty = float(config.refutation_penalty or 0.2)
        threshold = float(config.minor_confidence or 0.82)
        pending = self._store.list_proposals(status="pending", limit=500)
        by_id = {int(_row_field(row, "id") or 0): row for row in pending}
        rejected: List[int] = []
        for row in pending:
            source_id = int(_row_field(row, "id") or 0)
            for target_id in parse_id_list(_row_field(row, "contradicts")):
                if target_id in rejected:
                    continue
                target = by_id.get(target_id)
                if target is None:
                    continue
                if _row_field(target, "path") != _row_field(row, "path"):
                    continue
                confidence = apply_refutation_penalty(
                    float(_row_field(target, "confidence") or 0.0), penalty=penalty
                )
                self._store.update_proposal_confidence(target_id, confidence)
                if confidence >= threshold:
                    continue
                self._store.update_proposal_status(target_id, "rejected")
                self._store.append_promotion(
                    action="rejected",
                    target=_row_field(target, "target"),
                    path=_row_field(target, "path"),
                    old_value=_row_field(target, "proposed_value"),
                    new_value="",
                    reason=f"refuted_by:{source_id}",
                    proposal_id=target_id,
                    ts=self._iso(now),
                )
                self._count("refutations")
                rejected.append(target_id)
        return rejected

    # ── 关系确定性晋升 ──────────────────────────────────────────

    def promote_relationship(
        self, uid: str, scene_days: Set[str], *, now: datetime.datetime
    ) -> Dict[str, Any]:
        """关系确定性晋升（强约束 1 / 2 的落点）。

        Args:
            uid: 用户号（支线作用域）。
            scene_days: **调用方算好的**正向场景日集合（来自
                ``evidence.positive_signal_days``）。本类不自己取——避免
                ``continuity ↔ evidence`` 循环依赖。

        ⛔ 只动 ``RELATIONSHIP_AUTO_PROMOTE``（trust / closeness）：
        ``boundaries`` 冻结在锚定值；``stage`` 是派生结论（E13），不独立晋升。
        """
        config = self._plugin.config.promotion
        days = {str(day) for day in (scene_days or set())}
        value = relation_value(
            len(days),
            min_days=int(config.relation_min_days),
            days_per_step=int(config.relation_days_per_step),
            step=float(config.relation_step),
            max_value=float(config.relation_max),
        )
        engine = self._plugin._engine
        branch = engine.load_branch_state(uid)
        relationship = normalize_relationship(branch)
        changed: Dict[str, float] = {}
        for dimension in RELATIONSHIP_AUTO_PROMOTE:
            current = float(relationship.get(dimension) or 0.0)
            if value <= current:
                continue
            slow_set(branch, f"relationship.{dimension}", value, actor="promotion")
            self._store.append_promotion(
                action="applied",
                target="relationship",
                path=f"relationship.{dimension}",
                old_value=audit_text(current),
                new_value=audit_text(value),
                reason="positive_signal_days",
                source_uid=str(uid),
                ts=self._iso(now),
            )
            changed[dimension] = value
            self._count("promotions")
        if changed:
            relationship["stage"] = derive_stage(
                float(relationship.get("trust") or 0.0),
                float(relationship.get("closeness") or 0.0),
            )
            engine.save_branch_state(uid, branch)
        return {
            "uid": str(uid),
            "scene_days": len(days),
            "value": value,
            "changed": changed,
            "stage": relationship.get("stage"),
        }

    # ── 回滚（批 4-C8） ─────────────────────────────────────────

    def rollback_last(
        self, *, path: str = "", now: datetime.datetime
    ) -> Dict[str, Any]:
        """按 ``promotions`` 表逆序恢复**最后一次 applied** 的旧值。

        Args:
            path: 指定路径（如 ``perspective.world_view``）；空 → 取全表最近一条 applied。

        Returns:
            ``{"status": "rolled_back"|"nothing"|"missing_scope", ...}``。

        设计口径：
        - **只回滚一步**。慢变区是"小步可动、须留痕"的；一次撤多步会让「哪一步被撤了」
          无法叙述（留痕链断裂）。要连撤就多调几次，每步各自留痕。
        - 回滚后**冷却照常标记**：撤完立刻再晋升同一个值没有意义（会立刻被同一批证据
          推回去），留着冷却给人工介入的窗口。
        - ``relationship.*`` 的值是 per_user：必须靠审计行里的 ``source_uid`` 找到支线；
          缺 ``source_uid`` 时**拒绝执行**（宁可不动，也不猜是哪个用户）。
        """
        rows = self._store.list_promotions(path=path, limit=500)
        # 已撤销过的 applied 行**必须排除**，否则连调两次会一直撤销同一条
        # （audit 行不可变，故用 rolled_back 行的 ``rollback_of:<id>`` 反查已消费集合）。
        reverted = {
            int(str(row.get("reason") or "").split(":", 1)[1])
            for row in rows
            if str(row.get("action") or "") == "rolled_back"
            and str(row.get("reason") or "").startswith("rollback_of:")
            and str(row.get("reason") or "").split(":", 1)[1].isdigit()
        }
        target = next(
            (
                row
                for row in rows
                if str(row.get("action") or "") == "applied"
                and int(row.get("id") or 0) not in reverted
            ),
            None,
        )
        if target is None:
            return {"status": "nothing", "path": path}

        target_path = str(target.get("path") or "")
        old_value = coerce_slow_value(target_path, target.get("old_value"))
        head = target_path.split(".", 1)[0]
        engine = self._plugin._engine

        if head == "relationship":
            uid = str(target.get("source_uid") or "").strip()
            if not uid:
                # 拒绝瞎猜：关系值按 uid 隔离，不知道是谁就绝不能写
                return {
                    "status": "missing_scope",
                    "path": target_path,
                    "promotion_id": target.get("id"),
                }
            branch = engine.load_branch_state(uid)
            normalize_relationship(branch)
            slow_set(branch, target_path, old_value, actor="rollback")
            relationship = branch["relationship"]
            relationship["stage"] = derive_stage(
                float(relationship.get("trust") or 0.0),
                float(relationship.get("closeness") or 0.0),
            )
            engine.save_branch_state(uid, branch)
            scope = uid
        else:
            state = engine.load_self_state()
            perspective = normalize_perspective(state)
            slow_set(state, target_path, old_value, actor="rollback")
            perspective["origin"] = "rollback"
            perspective["updated_ts"] = self._iso(now)
            engine.save_self_state(state)
            scope = "self"

        self._store.append_promotion(
            action="rolled_back",
            target=str(target.get("target") or head),
            path=target_path,
            old_value=audit_text(target.get("new_value")),
            new_value=audit_text(old_value),
            reason=f"rollback_of:{target.get('id')}",
            source_uid=str(target.get("source_uid") or ""),
            proposal_id=target.get("proposal_id"),
            ts=self._iso(now),
        )
        proposal_id = target.get("proposal_id")
        if proposal_id is not None:
            self._store.update_proposal_status(int(proposal_id), "rolled_back")
        self.mark_cooldown(target_path, now)
        # 人可读留痕（与晋升同 kind；该 kind 被证据白名单拒，不会自证循环）
        self._store.append_chronicle(
            "self",
            "promotion",
            f"回滚（{target_path}）← 恢复旧值：{audit_text(old_value)}",
            ts=self._iso(now),
        )
        self._count("rollbacks")
        return {
            "status": "rolled_back",
            "path": target_path,
            "scope": scope,
            "restored": old_value,
            "promotion_id": target.get("id"),
            "proposal_id": proposal_id,
        }


__all__ = [
    "ANCHOR_FIELDS",
    "ANCHOR_ROOT_PREFIXES",
    "MERGE_MIN_VALUE_LEN",
    "PERSPECTIVE_FIELDS",
    "PROMOTION_ACTIONS",
    "PromotionEngine",
    "RELATIONSHIP_AUTO_PROMOTE",
    "RELATIONSHIP_DERIVED",
    "RELATIONSHIP_DIMENSIONS",
    "RELATIONSHIP_FACT_FIELDS",
    "SLOW_FIELD_AUDIENCE",
    "SLOW_FIELD_EXCLUDED_PREFIXES",
    "SLOW_FIELD_PATHS",
    "SLOW_WRITE_ACTORS",
    "apply_refutation_penalty",
    "assert_writable",
    "audit_text",
    "build_guard_keywords",
    "coerce_slow_value",
    "current_relationship_stage",
    "derive_stage",
    "filter_guarded_entries",
    "guard_blocks_entry",
    "guard_keywords",
    "guard_violations",
    "is_after_cooldown",
    "is_anchor_field",
    "is_slow_field",
    "is_slow_field_visible",
    "meets_major_gate",
    "meets_minor_gate",
    "merge_confidence",
    "normalize_perspective",
    "normalize_relationship",
    "parse_chronicle_refs",
    "parse_id_list",
    "relation_value",
    "scene_stats",
    "should_drop_output",
    "slow_get",
    "slow_set",
]
