"""kv 键命名空间登记表测试（深化 F / 架构体检候选 F）。

三层覆盖：
- **🔴 恒等断言**：常量值与 2026-10-08 收口前的现行字面量逐字节对比——
  键字符串不变是本批硬约束（存量数据零迁移），typo 即红；
- **分组完整性**：PERSONA_RESET ∪ KEEP ∪ 状态键 = 全清单、两分组不重叠——
  防将来新增命名空间漏登记（登记表驱动归零的正确性前提）；
- **`reset_for_new_persona` 全链**：摆满各类键 → 归零 → 清/留两侧断言 +
  编年史正文保留 + 事件队列清空 + self 重建带 schema_version。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_kvkeys.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import _synth_loader
from pytests._synth_loader import load  # noqa: E402
from pytests._synth_loader import FakeLogger, FakeStore, make_logger  # noqa: E402

_KVKEYS = load("services.kvkeys")
_ENGINE = load("services.state.engine")

reset_for_new_persona = _ENGINE.NarrativeEngine.reset_for_new_persona
default_self_state = _ENGINE.default_self_state

# ===== 🔴 恒等断言（与收口前字面量逐字节一致） =====


def test_constants_byte_identical_to_legacy():
    """常量值 = 现行字面量（2026-10-08 逐处抄录），任何改动即红。"""
    assert _KVKEYS.SELF_SCOPE == "self"
    assert _KVKEYS.BRANCH_PREFIX == "branch:"
    assert _KVKEYS.STREAM_MAP == "stream_map"
    assert _KVKEYS.GROUP_STREAM_MAP == "group_stream_map"
    assert _KVKEYS.CHRONICLE_DONE_PREFIX == "chronicle:"
    assert _KVKEYS.LIFE_FRAGMENT_LAST_TS == "life_fragment:last_ts"
    assert _KVKEYS.LIFE_FRAGMENT_COUNT == "life_fragment:count:"
    assert _KVKEYS.LIFE_FRAGMENT_HIGHLIGHT_COUNT == "life_fragment:highlight:count:"
    assert _KVKEYS.SEED_LAST_TRY == "seed:last_try"
    assert _KVKEYS.SEED_COUNT == "seed:count:"
    assert _KVKEYS.BYSOURCE_USED == "bysource:used:"
    assert _KVKEYS.BYSOURCE_ORIGIN == "bysource:origin:"
    assert _KVKEYS.BYSOURCE_SIGNED == "bysource:signed:"
    assert _KVKEYS.MILESTONE_CONSUMED == "milestone:consumed:"
    assert _KVKEYS.TOPIC_WEIGHT == "topic:weight:"
    assert _KVKEYS.TOPIC_PENDING == "topic:pending:"
    assert _KVKEYS.PROACTIVE_COUNT == "proactive:count:"
    assert _KVKEYS.PROPOSAL_LAST_TS == "proposal:last_ts"
    assert _KVKEYS.PROPOSAL_EMPTY == "proposal:empty:"
    assert _KVKEYS.PROMOTION_COOLDOWN == "promotion:cooldown:"
    assert _KVKEYS.STYLE_KEY == "style"


def test_legacy_module_names_unchanged():
    """消费模块本地名字不变（最小 diff 归一：删定义改 import）——引用点零改动。"""
    seeder = load("services.creation.seeder")
    assert seeder._SEED_LAST_TRY_KEY == "seed:last_try"
    assert seeder._SEED_COUNT_KEY == "seed:count:"
    sourcing = load("services.proactive.sourcing")
    assert sourcing._BYSOURCE_USED_KEY_PREFIX == "bysource:used:"
    assert sourcing._SIGN_COOLDOWN_KEY_PREFIX == "bysource:signed:"
    assert sourcing._MILESTONE_USED_KEY_PREFIX == "milestone:consumed:"
    assert sourcing._ORIGIN_KEY_PREFIX == "bysource:origin:"
    topic = load("services.learning.topic")
    assert topic._WEIGHT_PREFIX == "topic:weight:"
    assert topic._PENDING_KEY == "topic:pending:{uid}"
    engine = load("services.state.engine")
    assert engine._SELF_SCOPE == "self"
    health = load("services.learning.health")
    assert health.COOLDOWN_PREFIX == "promotion:cooldown:"


# ===== 分组完整性（登记表驱动的正确性前提） =====


def test_reset_groups_disjoint_and_complete():
    """PERSONA ∪ KEEP 不重叠；两者加上状态键 = 全清单（漏登记即红）。"""
    persona = set(_KVKEYS.PERSONA_RESET_PREFIXES)
    keep = set(_KVKEYS.KEEP_PREFIXES)
    all_keys = set(_KVKEYS.ALL_PREFIXES)
    assert not (persona & keep), "归零组与保留组不得重叠"
    # `self` 状态键经 reset_state 重建处理，不在任何前缀分组内
    assert all_keys == persona | keep | {_KVKEYS.SELF_SCOPE}, (
        "ALL_PREFIXES 必须恰为 PERSONA ∪ KEEP ∪ {self}——新增命名空间须先登记"
    )


def test_persona_reset_contains_expected_members():
    """归零组核心成员抽查：计数类键必须在（体检发现的 reset 漏清单问题）。"""
    persona = set(_KVKEYS.PERSONA_RESET_PREFIXES)
    for required in (
        _KVKEYS.BRANCH_PREFIX,
        _KVKEYS.LIFE_FRAGMENT_COUNT,
        _KVKEYS.LIFE_FRAGMENT_LAST_TS,
        _KVKEYS.LIFE_FRAGMENT_HIGHLIGHT_COUNT,
        _KVKEYS.SEED_COUNT,
        _KVKEYS.PROACTIVE_COUNT,
        _KVKEYS.TOPIC_WEIGHT,
        _KVKEYS.MILESTONE_CONSUMED,
        _KVKEYS.BYSOURCE_USED,
        _KVKEYS.STYLE_KEY,
        _KVKEYS.PROMOTION_COOLDOWN,
    ):
        assert required in persona, f"换人设归零组缺 {required!r}"


def test_keep_group_infrastructure():
    """保留组 = 会话基础设施 + 编年史幂等标记（append-only 纪律的配套键）。"""
    assert set(_KVKEYS.KEEP_PREFIXES) == {
        _KVKEYS.STREAM_MAP,
        _KVKEYS.GROUP_STREAM_MAP,
        _KVKEYS.CHRONICLE_DONE_PREFIX,
    }


# ===== reset_for_new_persona 全链 =====






def _make_engine():
    engine_mod = _ENGINE
    engine = engine_mod.NarrativeEngine.__new__(engine_mod.NarrativeEngine)
    store = FakeStore()
    engine._plugin = None  # 归零链不触 plugin
    engine._store = store
    engine._local_now = lambda: __import__("datetime").datetime(2026, 10, 8, 12, 0, 0)
    return engine, store


def _seed_all_key_families(store):
    """摆满全部登记键族的样本（含 KEEP 组），供归零后两侧断言。"""
    store.set_kv("self", {"state": {}})  # 会被重建
    store.set_kv("branch:10001", {"relationship": {}})
    store.set_kv("stream_map", {"10001": "s1"})
    store.set_kv("group_stream_map", {"987654": "g1"})
    store.set_kv_str("chronicle:self:life:2026-10-08", "1")
    store.set_kv_str("life_fragment:last_ts", "2026-10-08T11:00:00")
    store.set_kv_int("life_fragment:count:2026-10-08", 3)
    store.set_kv_int("life_fragment:highlight:count:2026-10-08", 1)
    store.set_kv_str("seed:last_try", "2026-10-08T11:00:00")
    store.set_kv_int("seed:count:2026-10-08", 1)
    store.set_kv_str("bysource:used:10001", "ts1")
    store.set_kv_str("bysource:origin:10001", "fragment")
    store.set_kv_str("bysource:signed:ev_x", "2026-10-08T11:00:00")
    store.set_kv_str("milestone:consumed:10001", "{}")
    store.set_kv("topic:weight:烤面筋", {"weight": 1.4})
    store.set_kv_str("topic:pending:10001", "烤面筋")
    store.set_kv_int("proactive:count:10001:2026-10-08", 2)
    store.set_kv_str("proposal:last_ts", "2026-10-08T10:00:00")
    store.set_kv_int("proposal:empty:2026-10-08", 1)
    store.set_kv_str("promotion:cooldown:perspective.world_view", "2026-10-08T10:00:00")
    store.set_kv("style", {"world_view": "旧的看法"})


def test_reset_for_new_persona_clears_and_keeps():
    """归零链：PERSONA 组全清 / KEEP 组保留 / self 重建带版本。"""
    engine, store = _make_engine()
    _seed_all_key_families(store)
    store.push_event({"scope": "branch:10001", "kind": "dialogue_material"})
    store.append_chronicle("self", "life", "旧生活的片段", "2026-10-07T12:00:00")

    summary = reset_for_new_persona(engine)

    # 清掉侧：人格相关业务态全清
    for gone in (
        "branch:10001",
        "life_fragment:last_ts",
        "life_fragment:count:2026-10-08",
        "life_fragment:highlight:count:2026-10-08",
        "seed:last_try",
        "seed:count:2026-10-08",
        "bysource:used:10001",
        "bysource:origin:10001",
        "bysource:signed:ev_x",
        "milestone:consumed:10001",
        "topic:weight:烤面筋",
        "topic:pending:10001",
        "proactive:count:10001:2026-10-08",
        "proposal:last_ts",
        "proposal:empty:2026-10-08",
        "promotion:cooldown:perspective.world_view",
        "style",
    ):
        assert gone not in store.kv_str, f"换人设归零应清掉 {gone!r}"

    # 保留侧（按读写面区分：会话映射走 JSON kv 面，chronicle 标记走 str 面）
    for kept in ("stream_map", "group_stream_map"):
        assert kept in store.kv, f"换人设归零应保留 {kept!r}"
    assert "chronicle:self:life:2026-10-08" in store.kv_str
    assert store.chronicle and store.chronicle[0]["text"] == "旧生活的片段"
    # 事件队列（涉私素材）必须清空
    assert store.events == []
    # self 重建为默认态（带 schema_version）
    rebuilt = store.get_kv("self")
    assert rebuilt is not None
    assert rebuilt["meta"]["version"] == default_self_state()["meta"]["version"]
    # 摘要可读
    assert summary["removed"] > 0 and summary["kept"] == 3


def test_reset_for_new_persona_idempotent():
    """再跑一次归零：前缀组幂等（无业务键可清，保留侧仍在，self 再次重建）。

    注：summary 的 removed 含 reset_state 对 self 键的既有计数（存在即计 1
    再重建，reset_state 原语义），故二次 removed == 1 而非 0。
    """
    engine, store = _make_engine()
    _seed_all_key_families(store)
    reset_for_new_persona(engine)
    before_mixin, before_str = dict(store.kv), dict(store.kv_str)
    summary2 = reset_for_new_persona(engine)
    # 除 self 重建（同值默认态）外，两个 kv 面均无任何变化
    assert {k: v for k, v in store.kv_str.items() if k != "self"} == {
        k: v for k, v in before_str.items() if k != "self"
    }
    assert {k: v for k, v in store.kv.items() if k != "self"} == {
        k: v for k, v in before_mixin.items() if k != "self"
    }
    assert summary2["removed"] == 1  # 仅 self 计数（reset_state 原语义）
    assert "stream_map" in store.kv


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
