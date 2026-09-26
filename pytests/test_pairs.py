"""互动配对（v0.2.0 批 4 · C2 / R31：**只建不消费**）。

锁定用户 2026-09-26 裁定的强约束 3 与其代码级形式：

1. **落点照建**：入站 pending 槽 + 出站配对落盘（批 5 的 LLM 提案要有米下锅）。
2. **晋升通道先不接**：``continuity.py`` / ``proposal.py`` / ``evidence.py``
   **禁止 import 本模块**（AST 断言，见文件末）。
3. **不猜消息 id**：只管 ``message_id`` / ``msg_id``；**不认**泛化的 ``id``
   （已知主动任务消息把 ``id`` 写成 ``proactive:<plugin>:<ts>``，误当 msg_id 会污染配对）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_pairs.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_pairs.py
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import _synth_loader

_PAIRS = _synth_loader.load("services.learning.pairs")

PairTracker = _PAIRS.PairTracker
message_id_of = _PAIRS.message_id_of
PLUGIN_ROOT = _synth_loader.PLUGIN_ROOT

_NOW = datetime.datetime(2026, 9, 20, 10, 0, 0)


class _FakeStore:
    """最小配对存储假件（``append_interaction_pair`` + ``count_interaction_pairs``）。"""

    def __init__(self, *, fail: bool = False):
        self._rows = []
        self._fail = fail

    def append_interaction_pair(
        self, uid, feedback_msg_id, response_msg_id, feedback_ts, response_ts
    ) -> bool:
        if self._fail:
            raise RuntimeError("磁盘写失败")
        key = (uid, feedback_msg_id)
        if any((row["uid"], row["feedback_msg_id"]) == key for row in self._rows):
            return False  # 唯一约束命中
        self._rows.append(
            {
                "uid": uid,
                "feedback_msg_id": feedback_msg_id,
                "response_msg_id": response_msg_id,
                "feedback_ts": feedback_ts,
                "response_ts": response_ts,
            }
        )
        return True

    def count_interaction_pairs(self, uid: str = "") -> int:
        normalized = str(uid or "").strip()
        if not normalized:
            return len(self._rows)
        return sum(1 for row in self._rows if row["uid"] == normalized)


# ─── 消息 id 提取（不猜） ───────────────────────────────────────


def test_message_id_of_prefers_message_id():
    assert message_id_of({"message_id": "m1", "msg_id": "m2"}) == "m1"
    assert message_id_of({"msg_id": "m2"}) == "m2"


def test_message_id_of_does_not_trust_generic_id():
    """**关键**：泛化 ``id`` 不算消息 id。

    主动任务消息会把 ``id`` 写成 ``proactive:<plugin>:<ts>``（已知坑），
    若把 ``id`` 当 msg_id，配对表会被这类伪 id 污染。
    """
    assert message_id_of({"id": "proactive:plugin:1690000000"}) == ""
    assert message_id_of({"id": "123", "text": "在吗"}) == ""


def test_message_id_of_tolerates_non_mapping_and_empty():
    assert message_id_of(None) == ""
    assert message_id_of("字符串") == ""
    assert message_id_of({}) == ""
    assert message_id_of({"message_id": "   "}) == ""


# ─── 入站 pending 槽 ────────────────────────────────────────────


def test_note_inbound_registers_pending():
    tracker = PairTracker(_FakeStore())
    assert tracker.note_inbound("u1", "m1", _NOW) is True
    pending = tracker.pending("u1")
    assert pending["msg_id"] == "m1"
    assert pending["ts"].startswith("2026-09-20T10:00")


def test_note_inbound_requires_uid_and_msg_id():
    tracker = PairTracker(_FakeStore())
    assert tracker.note_inbound("", "m1", _NOW) is False
    assert tracker.note_inbound("u1", "", _NOW) is False
    assert tracker.pending("u1") is None


def test_note_inbound_keeps_latest():
    """同一用户连续入站 → pending 只保留最近一条（配对的是「最近一次反馈」）。"""
    tracker = PairTracker(_FakeStore())
    tracker.note_inbound("u1", "m1", _NOW)
    tracker.note_inbound("u1", "m2", _NOW)
    assert tracker.pending("u1")["msg_id"] == "m2"


# ─── 出站配对落盘 ───────────────────────────────────────────────


def test_note_outbound_pairs_and_persists():
    store = _FakeStore()
    tracker = PairTracker(store)
    tracker.note_inbound("u1", "m1", _NOW)

    record = tracker.note_outbound("u1", "r1", _NOW)
    assert record["feedback_msg_id"] == "m1"
    assert record["response_msg_id"] == "r1"
    assert store.count_interaction_pairs("u1") == 1
    # pending 已被消费
    assert tracker.pending("u1") is None


def test_note_outbound_without_pending_returns_none():
    """没有待配对入站（如主动开口，用户还没说话）→ 不写垃圾配对。"""
    store = _FakeStore()
    tracker = PairTracker(store)
    assert tracker.note_outbound("u1", "r1", _NOW) is None
    assert store.count_interaction_pairs("u1") == 0


def test_note_outbound_allows_empty_response_id():
    """回应 id 未知仍落盘：这条记录仍证明「反馈已获回应」，批 5 会显式处理空值。"""
    store = _FakeStore()
    tracker = PairTracker(store)
    tracker.note_inbound("u1", "m1", _NOW)
    record = tracker.note_outbound("u1", "", _NOW)
    assert record is not None
    assert record["response_msg_id"] == ""


def test_note_outbound_is_idempotent_on_same_feedback():
    """同一句反馈重复配对 → store 侧唯一约束挡住，不堆积。"""
    store = _FakeStore()
    tracker = PairTracker(store)
    tracker.note_inbound("u1", "m1", _NOW)
    tracker.note_outbound("u1", "r1", _NOW)
    tracker.note_inbound("u1", "m1", _NOW)  # 同一条反馈又来一次
    tracker.note_outbound("u1", "r2", _NOW)
    assert store.count_interaction_pairs("u1") == 1


def test_note_outbound_swallows_store_failure():
    """配对是旁路：落盘失败只记 debug，**绝不拖垮主链路**（批 5 拿不到数据可接受，
    回复发不出去不可接受）。"""
    tracker = PairTracker(_FakeStore(fail=True))
    tracker.note_inbound("u1", "m1", _NOW)
    assert tracker.note_outbound("u1", "r1", _NOW) is None


def test_note_outbound_isolates_users():
    store = _FakeStore()
    tracker = PairTracker(store)
    tracker.note_inbound("u1", "m1", _NOW)
    tracker.note_inbound("u2", "m2", _NOW)
    tracker.note_outbound("u1", "r1", _NOW)
    # u2 的 pending 不被 u1 的出站消费
    assert tracker.pending("u2")["msg_id"] == "m2"
    assert store.count_interaction_pairs("u2") == 0


def test_clear_pending():
    tracker = PairTracker(_FakeStore())
    tracker.note_inbound("u1", "m1", _NOW)
    tracker.note_inbound("u2", "m2", _NOW)
    assert tracker.clear("u1") == 1
    assert tracker.clear() == 1
    assert tracker.clear() == 0


# ─── R31 的代码级隔离：晋升链路禁止 import 本模块 ───────────────


def _module_names_imported(path: Path) -> set:
    """收集一个 .py 文件 import 到的模块名（绝对/相对都归一到点分字符串）。"""
    if not path.exists():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_promotion_chain_never_imports_pairs_module():
    """**R31 的断言**：晋升链路三模块零 import ``pairs``。

    契约不是靠注释守的——「只建不消费」必须能被机器判定。任何一天有人在
    ``continuity.py`` 里顺手 ``from ..learning.pairs import PairTracker``，
    本测试立刻红。
    """
    chain = {
        "continuity": PLUGIN_ROOT / "services" / "state" / "continuity.py",
        "proposal": PLUGIN_ROOT / "services" / "learning" / "proposal.py",
        "evidence": PLUGIN_ROOT / "services" / "learning" / "evidence.py",
    }
    # 至少 continuity / evidence 必须存在（防路径写错导致断言空转）
    for name in ("continuity", "evidence"):
        assert chain[name].exists(), f"晋升链路模块缺失：{chain[name]}"

    for name, path in chain.items():
        imported = _module_names_imported(path)
        offenders = {mod for mod in imported if "pairs" in mod}
        assert not offenders, (
            f"{name} 不得 import 配对模块（R31：配对语义批 4 冻结、只建不消费），"
            f"实际 import 了 {sorted(offenders)}"
        )


def test_pairs_module_exists_at_expected_path():
    """本模块自身路径正确（否则上面的 AST 断言会因读不到文件而空转通过）。"""
    path = PLUGIN_ROOT / "services" / "learning" / "pairs.py"
    assert path.exists()
    assert "PairTracker" in path.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
