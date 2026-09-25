"""验收采样（对应 .scratch/narrative-persona/acceptance-dashboard.md 的 5 指标）。

指标名（写入 metrics/*.csv）：
- user_initiated_freq  用户主动发起频率（由谁起头标记）
- dialogue_depth       对话深度，按 scope 区分：user_msg_len/bot_msg_len=单条消息长度、
                       rounds=对话往返轮次（v0.1.4 补齐，入站登记+出站 30 分钟内配对成 1 轮）
- proactive_sent         主动开口**触发**计数（漏斗第一层，未确认送达）
- proactive_delivered    主动开口**确认送达**计数（承接率的真分母）
- proactive_undelivered  超窗仍未被确认送达的主动开口（沉默 / 发送失败）
- proactive_trigger_failed  触发被主程序拒绝（流不存在 / 未排队）
- proactive_replied      一次被接住的承接；**value = 延迟分钟数**（连续量）
- state_diversity        指标 4 **A 轨**：注入侧状态组合熵（随生活片段产出采集）
- output_diversity       指标 4 **B 轨**：产出侧文本多样性（字符 n-gram，n=3~5）
- proposals / promotions / refutations / rollbacks
                         晋升计数器四指标（R17，批 1 只建通道，批 4 才写入）

⚠️ 指标 4 双轨的读法：**A 变 B 不变 ＝ 只长数字不长故事**（状态在动，
但产出的故事还是那几句）。铺群闸门挂 B 轨（R8/P11），A 轨只作对照。
双轨只在**生活片段产出**这一刻同时采样，保证两侧同尺度可比。

⚠️ 2026-09-22 口径变更：`proactive_replied` 的 value 从恒 1 改为**延迟分钟**。
用 `count(*)` 统计次数的脚本不受影响；用 `sum(value)` 的脚本会失真（得到的是
总延迟分钟）。30min / 2h / 6h / 16h 等口径一律在分析层对 value 做筛选：
`count(*) where value <= 30`。原 `proactive_replied_24h` 已废弃——它与 30min
口径共用一条判定链，一条用户消息只会落进其中一个分支，导致 24h 分子被系统性
吞掉（A2 报告里的 24% 即由此而来，真实值 78%）。

成本侧：事件/编年史的额外 LLM token 由各调用点自行 record 到 `llm_extra_tokens`。

2026-09-13 体检（C5）：入站/出站的"何时记什么"判定（轮次配对、用户主动发起、
消息长度）自 plugin.py hook 内联逻辑下沉至此——指标口径集中在单一模块，
hook 只剩编排；跟踪状态的更新保持**无条件**（与下沉前一致），仅 CSV 写入受
telemetry.enabled 门控，A/B 对照窗口的采样口径不受影响。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

import datetime

from ..message import outbound_text_len
from ..store import NarrativeStore

# 用户消息与上一条 bot 消息的间隔超过该值，视为"用户主动发起"
_USER_INITIATED_GAP_MINUTES = 5
# 入站消息登记后，出站回复在该窗口内到达则配对为一次对话往返（指标 2；
# 与指标 3 的 30 分钟回复窗口保持同一时间尺度）
_ROUND_PAIR_WINDOW_MINUTES = 30
_STAGE_EPOCH = datetime.datetime(1970, 1, 1)

# ─── 指标 4 双轨（R24 A 轨 / R7 B 轨） ─────────────────────────
# 滚动窗口**只存内存**：多样性是统计量，重启丢一批样本对结论无影响，
# 不必为此往 kv 里塞文本（也不必让 store 为此新增接口）。
_DIVERSITY_WINDOW = 30
#: OBSERVE(R7/P5)：字符 n-gram 窗口 n=3~5（用户 2026-09-25 按推荐拍板）。
_NGRAM_RANGE = (3, 5)
#: 计数时剔除标点与空白——多样性看的是"换了什么说法"，不是标点抖动。
_PUNCT_PATTERN = re.compile(r"[\s\W_]+", re.UNICODE)

#: R17 晋升计数器的四种事件（批 1 建通道，批 4 由晋升机写入）
_COUNTER_KINDS = frozenset({"proposals", "promotions", "refutations", "rollbacks"})


def _normalize(text: str) -> str:
    """去标点与空白（n-gram 与组合键的归一化口径）。"""
    return _PUNCT_PATTERN.sub("", str(text or ""))


def ngram_diversity(
    texts: Sequence[str], n_min: int = _NGRAM_RANGE[0], n_max: int = _NGRAM_RANGE[1]
) -> float:
    """产出多样性 ∈ [0,1]：字符 n-gram 的**去重率**（distinct / total）。

    对 n ∈ [n_min, n_max] 各算一次再取平均——单一 n 值容易被"换汤不换药"
    的改写骗过（换掉几个字，n=5 的 gram 就全变了）。文本太短无法成 gram 时返回 0.0。
    """
    ratios: List[float] = []
    for size in range(n_min, n_max + 1):
        total = 0
        distinct: set = set()
        for raw in texts:
            normalized = _normalize(raw)
            for index in range(len(normalized) - size + 1):
                distinct.add(normalized[index : index + size])
                total += 1
        if total:
            ratios.append(len(distinct) / total)
    return sum(ratios) / len(ratios) if ratios else 0.0


def shannon_entropy(items: Iterable[Any]) -> float:
    """香农熵（bit）：取值越分散越大，全部相同为 0。样本不足 2 条时返回 0。"""
    counts = Counter(str(item) for item in items)
    total = sum(counts.values())
    if total < 2:
        return 0.0
    return -sum(
        (count / total) * math.log2(count / total) for count in counts.values()
    )


def state_combo_key(state: Dict[str, Any]) -> str:
    """状态组合键（A 轨的取值）：心情 × 作息相位 × 精力段。

    只取**注入侧**会被模型看到的维度——内部计数类字段的抖动不代表"生活有变化"。
    """
    inner = (state or {}).get("state", {})
    mood = inner.get("mood", {}) or {}
    routine = inner.get("routine", {}) or {}
    energy = float(mood.get("energy", 0.0))
    energy_band = int(min(9, max(0, energy * 10)))
    return f"{mood.get('label', '')}|{routine.get('phase', '')}|{energy_band}"


class Telemetry:
    """验收采样：入站/出站采样判定 + CSV 写入（telemetry.enabled 门控）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._store: NarrativeStore = plugin._store
        # stream_id -> 最后一次 bot 发送时刻（用户主动发起判定）
        self._last_bot_sent: Dict[str, datetime.datetime] = {}
        # stream_id -> 最近一次模式会话入站时刻（指标 2 对话轮次配对）
        self._pending_round: Dict[str, datetime.datetime] = {}
        # 指标 4 双轨的滚动样本（内存，有界 _DIVERSITY_WINDOW 条）
        self._diversity_combos: List[str] = []
        self._diversity_outputs: List[str] = []

    # ─── 入站/出站采样判定（hook 调用，判定逻辑单点在此） ────────

    def is_user_initiated(self, key: str, now: datetime.datetime) -> bool:
        """只读判定：距上一条 bot 消息超过阈值 = 用户主动发起。

        供指标 1（note_inbound）与 share_urge（plugin 入站 hook）共用口径，
        不改动任何跟踪状态。
        """
        last_sent = self._last_bot_sent.get(key, _STAGE_EPOCH)
        return (now - last_sent).total_seconds() / 60 > _USER_INITIATED_GAP_MINUTES

    def note_inbound(
        self,
        stream_id: str,
        user_id: str,
        text: str,
        now: datetime.datetime,
    ) -> None:
        """入站采样：轮次登记 + 用户主动发起判定 + 入站长度（指标 1/2）。"""
        key = stream_id or user_id
        # 指标 2 · 对话轮次：登记本轮入站，待出站回复配对成一次往返（scope=rounds）
        self._pending_round[key] = now

        # 验收指标 1：用户主动发起（距上一条 bot 消息超过阈值）
        if self.is_user_initiated(key, now):
            self.record("user_initiated_freq", 1, user_id=user_id)
        # 验收指标 2：入站消息长度
        self.record("dialogue_depth", value=float(len(text)), user_id=user_id, scope="user_msg_len")

    def note_outbound(
        self,
        stream_id: str,
        user_id: str,
        message: Any,
        now: datetime.datetime,
    ) -> None:
        """出站采样：出站时刻登记 + 轮次配对 + bot 消息长度（指标 1/2 对照侧）。"""
        if stream_id:
            self._last_bot_sent[stream_id] = now
            # 指标 2 · 对话轮次：本条出站若在窗口内接住一次模式会话入站，记一轮往返
            pending_ts = self._pending_round.pop(stream_id, None)
            if pending_ts is not None and (
                (now - pending_ts).total_seconds() <= _ROUND_PAIR_WINDOW_MINUTES * 60
            ):
                # user_id 由调用方（hook）经 StreamRegistry 反查传入，支持按用户拆轮次
                self.record("dialogue_depth", value=1, scope="rounds", user_id=user_id)
        bot_text_len = outbound_text_len(message)
        if bot_text_len is not None:
            self.record("dialogue_depth", value=float(bot_text_len), scope="bot_msg_len")

    # ─── 指标 4 双轨（A=注入侧组合熵 / B=产出侧文本多样性） ───────

    def note_fragment(self, state: Dict[str, Any], text: str) -> None:
        """生活片段产出采样：同一次产出同时记 A、B 两轨（保证同尺度可比）。"""
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return
        self._diversity_combos.append(state_combo_key(state))
        self._diversity_outputs.append(normalized_text)
        self._diversity_combos = self._diversity_combos[-_DIVERSITY_WINDOW:]
        self._diversity_outputs = self._diversity_outputs[-_DIVERSITY_WINDOW:]
        # A 轨（R24）：状态组合熵
        self.record("state_diversity", value=shannon_entropy(self._diversity_combos), scope="combo_entropy")
        # B 轨（R7）：产出文本 n-gram 去重率——铺群闸门挂这条（R8/P11）
        self.record(
            "output_diversity",
            value=ngram_diversity(self._diversity_outputs),
            scope=f"ngram{_NGRAM_RANGE[0]}-{_NGRAM_RANGE[1]}",
        )

    # ─── 晋升计数器通道（R17，批 1 只建通道，批 4 才写入） ────────

    def record_counter(self, kind: str, value: float = 1) -> None:
        """晋升链路计数器：proposals / promotions / refutations / rollbacks。

        四者各落一个 csv（``metrics/{kind}.csv``），告警线待实机数据定（P12）。
        ⚠ 未登记的 kind 会被拒——计数器是给人看的，写错了比不写更糟。
        """
        if kind not in _COUNTER_KINDS:
            raise ValueError(f"未知的晋升计数器类型: {kind!r}（可选 {sorted(_COUNTER_KINDS)}）")
        self.record(kind, value=value, scope="promotion")

    def clear_pending_rounds(self) -> None:
        """清空待配对轮次登记（状态重置用；_last_bot_sent 保留，与原 reset 语义一致）。"""
        self._pending_round.clear()

    # ─── 通用采样 ────────────────────────────────────────────────

    def record(
        self,
        name: str,
        value: float = 1,
        user_id: str = "",
        scope: str = "",
    ) -> None:
        """追加一条采样；总开关关闭时静默跳过。"""
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.telemetry.enabled:
            return
        try:
            self._store.append_metric(
                name=name,
                value=float(value),
                user_id=str(user_id or ""),
                scope=str(scope or ""),
            )
        except Exception as exc:  # 采样失败不影响主流程
            self._plugin.ctx.logger.debug("指标采样失败: %s", exc)

    def record_llm_tokens(self, tokens: int, task: str = "creation") -> None:
        """记录剧本相关额外 LLM token（成本指标）。"""
        self.record("llm_extra_tokens", value=float(tokens), scope=f"task:{task}")


__all__ = [
    "Telemetry",
    "ngram_diversity",
    "shannon_entropy",
    "state_combo_key",
]
