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


__all__ = [
    "ANCHOR_FIELDS",
    "ANCHOR_ROOT_PREFIXES",
    "RELATIONSHIP_DERIVED",
    "RELATIONSHIP_DIMENSIONS",
    "RELATIONSHIP_FACT_FIELDS",
    "SLOW_FIELD_AUDIENCE",
    "SLOW_FIELD_EXCLUDED_PREFIXES",
    "SLOW_FIELD_PATHS",
    "assert_writable",
    "build_guard_keywords",
    "current_relationship_stage",
    "filter_guarded_entries",
    "guard_blocks_entry",
    "guard_keywords",
    "guard_violations",
    "is_anchor_field",
    "is_slow_field",
    "is_slow_field_visible",
    "normalize_relationship",
    "should_drop_output",
]
