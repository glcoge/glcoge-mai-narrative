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

from typing import Any, Dict, FrozenSet, Iterable, List, Set, Tuple

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


def _keyword_fragments(text: str) -> List[str]:
    """把一条规则/价值观原文切成候选关键词（保守、可预期）。"""
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
    return [
        fragment
        for fragment in fragments
        if _KEYWORD_MIN_LEN <= len(fragment) <= _KEYWORD_MAX_LEN
    ]


__all__ = [
    "ANCHOR_FIELDS",
    "ANCHOR_ROOT_PREFIXES",
    "RELATIONSHIP_DERIVED",
    "RELATIONSHIP_DIMENSIONS",
    "RELATIONSHIP_FACT_FIELDS",
    "SLOW_FIELD_AUDIENCE",
    "SLOW_FIELD_EXCLUDED_PREFIXES",
    "SLOW_FIELD_PATHS",
    "current_relationship_stage",
    "guard_keywords",
    "is_anchor_field",
    "is_slow_field",
    "is_slow_field_visible",
    "normalize_relationship",
]
