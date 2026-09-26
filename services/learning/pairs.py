"""互动配对（v0.2.0 批 4 · C2）——R31：**只建不消费**。

本模块是「配对语义」的唯一载体：把「用户反馈消息 id」与「角色**实际送达**的回应
消息 id」链接成一条可引用的记录，服务**批 5** 的 relationship LLM 提案
（ADR-0002 证据纪律 4：关系晋升必须带真实反馈→已送达配对的证据链）。

用户 2026-09-26 裁定（批 4 方案 §0.5 强约束 3）：

- **只建不消费**：落点照做，晋升通道先不接。服务器恢复实机后数据即开始累积，
  批 5 直接有米下锅。只建不消费 = 零风险 + 零恒空，比「要不要建」的争论便宜。
- **晋升链路禁止 import 本模块**：由 ``pytests/test_pairs.py`` 的 AST 断言锁住
  （``continuity.py`` / ``proposal.py`` / ``evidence.py`` 都不得出现本模块）。

⚠️ 与批 4 的「事件级正向信号」分居两处，**刻意不同表**：正向信号走
``metrics/*.csv``（事件记录），配对走 ``interaction_pairs`` 表（链接关系）。
两者语义不同，混存会让「晋升读了带配对语义的行」变成一条难以审计的暗路。

⚠️ 载荷键名待实机核对（R31）：宿主入站/出站消息里的消息 id 键名无法在服务器离线
期间确认，故 ``message_id_of`` 只认**语义明确**的 ``message_id`` / ``msg_id``，
**不认**泛化的 ``id``——已知 ``id`` 会被主动任务消息写成
``proactive:<plugin>:<ts>``，误当 msg_id 会污染配对。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional

#: 可信的消息 id 键名（按顺序取第一个非空）。
#: 刻意**不含** ``id``：歧义太大且有已知误用先例（见模块 docstring）。
_MESSAGE_ID_KEYS = ("message_id", "msg_id")


def message_id_of(message: Any) -> str:
    """从宿主消息载荷里取消息 id；取不到返回空串（**不猜**）。

    取不到就不配对——写一行 ``response_msg_id=''`` 的垃圾配对，比不写更糟：
    它会让批 5 的「回应晚于反馈」校验拿到假数据。
    """
    if not isinstance(message, Mapping):
        return ""
    for key in _MESSAGE_ID_KEYS:
        value = str(message.get(key) or "").strip()
        if value:
            return value
    return ""


def _iso(now: Optional[datetime]) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


class PairTracker:
    """入站/出站配对追踪（内存 pending 槽 + store 落盘）。

    pending 槽**刻意放内存**：它只承载「本轮入站还没被回应」这一瞬态。重启会丢，
    但重启后那一轮本就断了（回应的前提是同一进程内的连续会话），
    为它引入持久化是给一个不存在的问题付成本。
    """

    def __init__(self, store: Any, *, logger: Any = None) -> None:
        self._store = store
        self._logger = logger
        #: uid -> {"msg_id": str, "ts": iso}
        self._pending: Dict[str, Dict[str, str]] = {}

    # ─── 入站：登记待配对 ───────────────────────────────────────

    def note_inbound(self, uid: str, msg_id: str, now: Optional[datetime] = None) -> bool:
        """登记一条待配对的用户反馈；缺 uid 或缺 msg_id 时静默不记。

        Returns:
            bool: True=已登记。
        """
        uid_text = str(uid or "").strip()
        mid = str(msg_id or "").strip()
        if not uid_text or not mid:
            return False
        self._pending[uid_text] = {"msg_id": mid, "ts": _iso(now)}
        return True

    # ─── 出站：与最近待配对的入站配对并落盘 ─────────────────────

    def note_outbound(
        self, uid: str, response_msg_id: str, now: Optional[datetime] = None
    ) -> Optional[Dict[str, str]]:
        """把最近一条待配对的入站反馈与本轮出站回应配对落盘。

        Returns:
            配对记录 dict（已落盘）；无待配对入站或缺 uid 时返回 None。

        ``response_msg_id`` 为空时**仍然落盘**：这条记录仍是有效的「反馈已获回应」
        事实（只是回应 id 未知），且批 5 的校验会显式处理空回应 id 的情形。
        """
        uid_text = str(uid or "").strip()
        if not uid_text:
            return None
        pending = self._pending.pop(uid_text, None)
        if pending is None:
            return None
        record = {
            "uid": uid_text,
            "feedback_msg_id": pending["msg_id"],
            "response_msg_id": str(response_msg_id or "").strip(),
            "feedback_ts": pending["ts"],
            "response_ts": _iso(now),
        }
        try:
            self._store.append_interaction_pair(
                uid=record["uid"],
                feedback_msg_id=record["feedback_msg_id"],
                response_msg_id=record["response_msg_id"],
                feedback_ts=record["feedback_ts"],
                response_ts=record["response_ts"],
            )
        except Exception as exc:  # noqa: BLE001 —— 配对是旁路，绝不拖垮主链路
            if self._logger is not None:
                self._logger.debug("互动配对落盘失败（忽略）: %s", exc)
            return None
        return record

    # ─── 查询 / 维护 ────────────────────────────────────────────

    def pending(self, uid: str) -> Optional[Dict[str, str]]:
        """查看某用户当前待配对的入站（调试/断言用）。"""
        return self._pending.get(str(uid or "").strip())

    def clear(self, uid: str = "") -> int:
        """清空 pending 槽（``uid`` 为空则全清）；返回清除条数。"""
        normalized = str(uid or "").strip()
        if not normalized:
            removed = len(self._pending)
            self._pending.clear()
            return removed
        return 1 if self._pending.pop(normalized, None) is not None else 0

    def count_stored(self, uid: str = "") -> int:
        """落盘配对条数（透传 store，供断言与状态展示）。"""
        return int(self._store.count_interaction_pairs(uid))


__all__ = ["PairTracker", "message_id_of"]
