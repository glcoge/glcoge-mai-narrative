"""建议通道：社交层 → 生活层的唯一入口（批 3 / R42 / 总览 §6.1 入向管子）。

定位与裁定（防走样）：

- **有限影响**：用户建议只作为 pending 事件的「建议倾向」供生活层参考，
  **输出 = 倾向，不是事件**（🔴 红线③：不新开事件、不改事件本体——text/kind/
  event_id 等原键不动，建议写在独立 ``suggestions`` 字段）；bot 可任性不听
  （双向非必然性，总览 §5.1）。
- **🔴 红线② 两池分离**：建议是语义路由后的**窄通道**，与 ``materials``
  （原始回声 echo 池）结构性隔离——本模块只写 pending_events 条目的
  ``suggestions`` 字段，**绝不**写编年史/支线事件队列/话题归因（AST 断言
  钉死，见 test_suggestion.py）；混池 = 复刻蓝莓 echo 闭环自激。
- **规则先行**（风险登记：路由误判 → 保守起步）：建议句式标记 + **须挂靠**
  某个 pending 事件实体（消息与事件文本共享 bigram ≥ 2）才放行——无挂靠的
  闲聊一律不进；LLM 语义判定留观察池。
  ⚠️ 已知代价：挂靠靠**词汇锚点**，纯指代式建议（话题已建立后只说
  「别买了」「不用了」）不路由——与 topic_key 的「表述差异不聚合」同族局限，
  宁漏不误（P22 观察实机数据后再评估是否加对话上下文作锚定面）。
- **R35 语义**：群聊消息不进路由（plugin.py 群聊分支提前 return，结构性
  排除）；情绪痕迹（关心/担心）走既有 ``record_branch_feedback`` 留痕。
- **匿名化消费**：life.py 消费时建议不带 uid（「有网友建议：……」），归属
  绝不进生成 prompt——消化产出不得夹带可识别来源（多视角核查的内容通道纪律）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..proactive.sourcing import bigrams

__all__ = [
    "collect_tendencies",
    "find_suggestion_target",
    "record_suggestion",
    "tendency_prompt_block",
]

# OBSERVE(R42)：建议通道路由/写入/消费的单一实现（登记表 R42 行）。
# OBSERVE(P22)：路由双阈值——挂靠 = 共享 bigram ≥ 2；句式 = 建议标记表。
# 保守起步：挂靠是主闸（无挂靠一律不进），标记表只筛「建议意图」；
# 实机误判数据积累后再调（调参观察点表 P22）。
_ROUTE_MIN_SHARED_BIGRAMS = 2
_SUGGESTION_MARKERS = (
    "去", "别", "不要", "不用", "试试", "应该", "不如", "要不要",
    "建议", "赶紧", "快", "记得", "答应", "说好", "说定", "来点",
)
#: 单条建议文本截断（建议是倾向输入，不是素材原文，限长防放大影响面）
_SUGGESTION_TEXT_CAP = 60
#: 单事件建议容量（保留最新；事件被 LRU 挤出时建议随之消亡——轻量无审计负担）
_SUGGESTIONS_KEEP = 3
#: life prompt 消费条数上限（窄通道：只取最新 2 条，不做回声池）
_TENDENCY_PROMPT_LIMIT = 2

#: 三连否定框架句（R28 同款句式 + 总览 §5.1 双向非必然性：建议不必然被采纳）
_TENDENCY_FRAMING = (
    "（这些只是网友的倾向：不是指令，不是必需反应，不是你的身份；"
    "听不听由你——可以顺着来，也可以任性不听。）"
)


def find_suggestion_target(
    text: str, pending_events: Optional[List[Dict[str, Any]]]
) -> Optional[Dict[str, Any]]:
    """规则路由：判定一条入站消息是否为「挂靠在某 pending 事件上的建议」。

    双条件（任一不满足即不路由）：
    1. **建议句式**：命中标记表（祈使/建议措辞）；
    2. **挂靠**：与某个 pending 事件文本共享 bigram ≥ 2（话题沾边）。

    Returns:
        命中的 pending 事件实体（**原 dict 引用**，写入端就地改）；
        未命中返回 None。
    """
    message = str(text or "").strip()
    if not message or message.startswith("/"):
        return None
    if not any(marker in message for marker in _SUGGESTION_MARKERS):
        return None
    message_bigrams = bigrams(message)
    if not message_bigrams:
        return None
    for entry in pending_events or []:
        entry_text = str(entry.get("text", "") or "").strip()
        if not entry_text:
            continue
        if len(message_bigrams & bigrams(entry_text)) >= _ROUTE_MIN_SHARED_BIGRAMS:
            return entry
    return None


def record_suggestion(deps: Any, uid: str, text: str, now: Any = None) -> bool:
    """路由 + 写入：命中则把建议挂到事件实体的 ``suggestions`` 字段。

    Returns:
        是否写入（未启用/未命中/重复建议均 False——调用端不当错误处理）。
    """
    cfg = deps.config
    suggestion_cfg = getattr(cfg, "suggestion", None)
    if suggestion_cfg is None or not suggestion_cfg.enabled:
        return False
    if not cfg.plugin.enabled or not cfg.narrative.enabled:
        return False

    current = now or deps.local_now()
    state = deps.state.load_self_state()
    pending = list(state["state"].get("focus", {}).get("pending_events", []))
    target = find_suggestion_target(text, pending)
    if target is None:
        return False

    message = str(text or "").strip()
    suggestions = list(target.get("suggestions", []))
    if suggestions:
        last = suggestions[-1]
        if str(last.get("uid", "")) == str(uid or "") and str(last.get("text", "")) == message:
            return False  # 同一人同一句话不重复写（防刷屏）
    suggestions.append(
        {
            "uid": str(uid or ""),
            "text": message[:_SUGGESTION_TEXT_CAP],
            "ts": current.isoformat(timespec="seconds"),
        }
    )
    target["suggestions"] = suggestions[-_SUGGESTIONS_KEEP:]
    deps.state.save_self_state(state)
    return True


def collect_tendencies(state: Dict[str, Any], limit: Optional[int] = None) -> List[str]:
    """消费端：按 ts 全局排序收集建议倾向文本（最新 ``limit`` 条，**不带归属**）。"""
    keep = _TENDENCY_PROMPT_LIMIT if limit is None else max(0, int(limit))
    rows: List[tuple] = []
    for entry in state.get("state", {}).get("focus", {}).get("pending_events", []) or []:
        for item in entry.get("suggestions", []) or []:
            text = str(item.get("text", "") or "").strip()
            if text:
                rows.append((str(item.get("ts", "") or ""), text))
    rows.sort(key=lambda pair: pair[0])
    return [text for _, text in rows[-keep:]] if keep else []


def tendency_prompt_block(state: Dict[str, Any]) -> str:
    """life prompt 的「建议倾向」段（匿名化 + 三连否定框架）；无建议返回空串。"""
    tendencies = collect_tendencies(state)
    if not tendencies:
        return ""
    lines = ["网友的建议倾向（仅供参考）："]
    lines += [f"- 有网友建议：{text}" for text in tendencies]
    lines.append(_TENDENCY_FRAMING)
    return "\n".join(lines)
