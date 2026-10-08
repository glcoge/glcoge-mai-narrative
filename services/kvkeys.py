"""kv 键命名空间登记表（深化 F / 架构体检候选 F）——kv 键的**单一事实源**。

为什么要有本模块（架构体检 H6 的摩擦）：全部 22 个命名空间此前散布 12 个文件，
一半常量一半内联字面量（``proactive:count:*`` 由 scheduler 与 plugin 两文件各拼、
``promotion:cooldown:*`` 双模块共享），没有任何登记处知道「全部键空间」——后果是
``reset_state``（换代归零）的清单漏掉全部计数类键，换人设时静默残留旧数据。

本模块三个职责：

1. **常量收口**：全部前缀常量集中定义，值与收口前字面量**逐字节一致**
   （存量数据零迁移；各消费模块删本地定义、改 import，模块内名字不变）；
2. **归零分组登记**：``PERSONA_RESET_PREFIXES``（换人设归零 = 全新生活的开始）
   与 ``KEEP_PREFIXES``（会话基础设施 + 编年史配套，与人格无关）——
   ``reset_for_new_persona`` 由登记表驱动，新增命名空间必须先在此登记
   （测试锁 ``ALL == PERSONA ∪ KEEP ∪ {self}``）；
3. **换代与归零的语义分界**（不改既有行为，只把语义写明）：
   - ``reset_state()``（R12 换代）：只清 ``branch:`` + ``self``——换代=结构迁移，
     业务键保留；
   - ``reset_for_new_persona()``（闸门 A 换人设）：PERSONA 组全清 + self 重建 +
     支线事件队列清空；KEEP 组与编年史正文保留；
   - ``/narrative reset yes``：用户全清（毯式，含 stream_map——现有语义保留）。

零依赖（不 import 任何 services 模块）——全层可安全引用，无循环导入。
"""

from __future__ import annotations

__all__ = [
    # 状态键（经 reset_state 重建，不走前缀清除）
    "SELF_SCOPE",
    # 前缀 / 键常量（值与收口前字面量逐字节一致，🔴 不可改动）
    "BRANCH_PREFIX",
    "STREAM_MAP",
    "GROUP_STREAM_MAP",
    "CHRONICLE_DONE_PREFIX",
    "LIFE_FRAGMENT_LAST_TS",
    "LIFE_FRAGMENT_COUNT",
    "LIFE_FRAGMENT_HIGHLIGHT_COUNT",
    "SEED_LAST_TRY",
    "SEED_COUNT",
    "BYSOURCE_USED",
    "BYSOURCE_ORIGIN",
    "BYSOURCE_SIGNED",
    "MILESTONE_CONSUMED",
    "TOPIC_WEIGHT",
    "TOPIC_PENDING",
    "PROACTIVE_COUNT",
    "PROPOSAL_LAST_TS",
    "PROPOSAL_EMPTY",
    "PROMOTION_COOLDOWN",
    "STYLE_KEY",
    # 分组与迭代
    "PERSONA_RESET_PREFIXES",
    "KEEP_PREFIXES",
    "ALL_PREFIXES",
    "iter_prefixes",
]

# ─── 状态键 ──────────────────────────────────────────────────────

#: 自我层状态（JSON，整键非前缀；归零经 reset_state 重建默认态，带 schema_version）
SELF_SCOPE = "self"

# ─── 前缀 / 键常量（🔴 值 = 收口前字面量，改动即破坏存量数据） ────

#: 支线层状态（JSON，per-user）
BRANCH_PREFIX = "branch:"
#: uid ↔ stream 会话映射（JSON；**归零保留**——换人设不清，防主动消息失联）
STREAM_MAP = "stream_map"
#: gid ↔ session 群会话映射（JSON；同上保留）
GROUP_STREAM_MAP = "group_stream_map"
#: 编年史当日幂等标记（chronicle:{scope}:{kind}:{date}；append-only 的配套键，保留）
CHRONICLE_DONE_PREFIX = "chronicle:"

#: 生活片段：间隔闸 / 日计数 / 高光日上限（P19 冻结区——常量引用替换，读写语义不动）
LIFE_FRAGMENT_LAST_TS = "life_fragment:last_ts"
LIFE_FRAGMENT_COUNT = "life_fragment:count:"
LIFE_FRAGMENT_HIGHLIGHT_COUNT = "life_fragment:highlight:count:"

#: 世界事件播种：间隔闸 / 日计数（批 2 / R40）
SEED_LAST_TRY = "seed:last_try"
SEED_COUNT = "seed:count:"

#: 由头：去复用（per-uid）/ 3 选 1 来源队列（per-uid）/ P4 跨用户签发冷却（per-event）
BYSOURCE_USED = "bysource:used:"
BYSOURCE_ORIGIN = "bysource:origin:"
BYSOURCE_SIGNED = "bysource:signed:"

#: 里程碑条目冷却（per-uid JSON，R31/P21）
MILESTONE_CONSUMED = "milestone:consumed:"

#: 话题权重（JSON）/ 待归因话题（R16/P10）
TOPIC_WEIGHT = "topic:weight:"
TOPIC_PENDING = "topic:pending:"

#: 主动开口日计数（per-uid per-date）
PROACTIVE_COUNT = "proactive:count:"

#: 慢变提案：间隔闸 / 当日空转标记（R37 提炼节奏）
PROPOSAL_LAST_TS = "proposal:last_ts"
PROPOSAL_EMPTY = "proposal:empty:"

#: 慢变晋升冷却（per-path；continuity 写、health 读——曾双模块各自拼，F 收口）
PROMOTION_COOLDOWN = "promotion:cooldown:"

#: [learned] 风格投影（R1；config 只是投影，事实源在 sqlite——此 kv 键为投影缓存位）
STYLE_KEY = "style"


# ─── 归零分组（登记表驱动的核心） ────────────────────────────────

#: 换人设归零组：人格相关的全部业务态——全新生活的开始，旧数据一律不留。
#: 新增命名空间时按语义归组；测试锁「ALL == PERSONA ∪ KEEP ∪ {SELF}」防漏登记。
PERSONA_RESET_PREFIXES = (
    BRANCH_PREFIX,
    LIFE_FRAGMENT_LAST_TS,
    LIFE_FRAGMENT_COUNT,
    LIFE_FRAGMENT_HIGHLIGHT_COUNT,
    SEED_LAST_TRY,
    SEED_COUNT,
    BYSOURCE_USED,
    BYSOURCE_ORIGIN,
    BYSOURCE_SIGNED,
    MILESTONE_CONSUMED,
    TOPIC_WEIGHT,
    TOPIC_PENDING,
    PROACTIVE_COUNT,
    PROPOSAL_LAST_TS,
    PROPOSAL_EMPTY,
    PROMOTION_COOLDOWN,
    STYLE_KEY,
)

#: 换人设保留组：会话基础设施（bot 与谁有会话，与人格无关）+ 编年史幂等标记。
KEEP_PREFIXES = (
    STREAM_MAP,
    GROUP_STREAM_MAP,
    CHRONICLE_DONE_PREFIX,
)

#: 全量命名空间（状态键 + 全部前缀）；分组完整性测试的基准。
ALL_PREFIXES = (SELF_SCOPE,) + PERSONA_RESET_PREFIXES + KEEP_PREFIXES


def iter_prefixes() -> tuple:
    """按登记顺序迭代全部前缀（巡检/诊断工具用）。"""
    return PERSONA_RESET_PREFIXES + KEEP_PREFIXES
