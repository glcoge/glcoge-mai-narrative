"""R35 群聊观察 —— 隔离纪律与群注入边界的回归锁。

为什么必须有这份测试：群里的内容一旦流进私聊，就是 09-21 泄露事故的重演，
而且**没有任何报错**——你会看到 bot 在私聊里提到群友说过的话，无从追溯。
所以本文件锁的全部都是 fail-closed 性质：

- 群号取不到 → **拒绝落库**（不是「降级当私聊素材」）
- 群不在观察名单 → 拒绝落库
- ``source_uid`` 必须形如 ``g:<gid>``（漏传时靠 store 侧反推兜底）
- 群聊**不碰**任何私聊语义链路（支线 / 互动时点 / telemetry / 承接 / 配对）
- 群注入**不得夹带**关系语境与私聊学到的表达风格

运行（插件目录）：

    ../../.venv/Scripts/python.exe -m pytest pytests/test_group_observe.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import _synth_loader

# ⚠️ 必须先 load("services") 再 load("plugin")：合成包的 services 占位模块
# 只有真正执行过 __init__.py 才有导出的名字，否则 plugin.py 的
# ``from .services import ...`` 会拿到空壳模块。
_SERVICES = _synth_loader.load("services")

_MESSAGE = _synth_loader.load("services.message")
_STORE = _synth_loader.load("services.store")
_STREAMS = _synth_loader.load("services.streams")
_ENGINE_MOD = _synth_loader.load("services.state.engine")
_REPLYER = _synth_loader.load("services.render.replyer_block")
_PLUGIN = _synth_loader.load("plugin")

extract_group_id = _MESSAGE.extract_group_id
NarrativeStore = _STORE.NarrativeStore
GroupStreamRegistry = _STREAMS.GroupStreamRegistry
NarrativeEngine = _ENGINE_MOD.NarrativeEngine
MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin

GID = "88886666"
OTHER_GID = "77775555"
GROUP_STREAM = "group-stream-001"


def _tmp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="narrative-group-"))


def _group_message(text: str = "在吗", gid: str = GID) -> dict:
    """构造宿主真实形态的群聊载荷（``serialize_session_message`` 产物）。"""
    return {
        "message_id": "m-1",
        "timestamp": "1760000000.0",
        "platform": "qq",
        "session_id": GROUP_STREAM,
        "processed_plain_text": text,
        "is_command": False,
        "is_notify": False,
        "message_info": {
            "user_info": {"user_id": "2111957354", "user_nickname": "某人"},
            "group_info": {"group_id": gid, "group_name": "测试群"},
        },
        "raw_message": [{"type": "text", "data": text}],
    }


def _private_message(text: str = "在吗") -> dict:
    return {
        "session_id": "pv-1",
        "processed_plain_text": text,
        "message_info": {
            "user_info": {"user_id": "10001", "user_nickname": "某人"},
            "group_info": None,
        },
        "raw_message": [{"type": "text", "data": text}],
    }


# ─── 1. 群号提取 seam（与 is_private_chat 同判定，不得漂移） ───


def test_extract_group_id_from_group_message():
    assert extract_group_id(_group_message()) == GID


def test_extract_group_id_returns_empty_when_not_group():
    """私聊 / None / 空 dict / 结构缺失一律空串 —— truthy 判定不得漂移。"""
    assert extract_group_id(_private_message()) == ""
    assert extract_group_id({}) == ""
    assert extract_group_id({"message_info": {}}) == ""
    assert extract_group_id({"message_info": {"group_info": None}}) == ""
    assert extract_group_id({"message_info": {"group_info": {}}}) == ""


# ─── 2. store 双保险打标（R35 的隐私底线） ───


def test_push_event_group_scope_derives_gid_tag_when_omitted():
    """漏传 source_uid 时也必须带上 ``g:<gid>``，绝不能退化成「通用素材」。"""
    store = NarrativeStore(_tmp_dir())
    store.push_event({"scope": f"group:{GID}", "bysource": "群里的话"})
    assert store.list_events(f"group:{GID}", limit=5)[0]["source_uid"] == f"g:{GID}"


def test_push_event_rejects_untagged_group_scope():
    """空群号的 group 作用域无法构成受众标 → 拒绝写入（fail-closed）。"""
    store = NarrativeStore(_tmp_dir())
    store.push_event({"scope": "group:", "bysource": "不知道哪个群"})
    assert store.list_events("group:", limit=5) == []


def test_group_material_kind_is_not_evidence_eligible():
    """``group_material`` 永不进晋升证据池（三重保险之一）。"""
    store = NarrativeStore(_tmp_dir())
    store.push_event({"scope": f"group:{GID}", "kind": "group_material", "bysource": "甲"})
    event = store.list_events(f"group:{GID}", limit=5)[0]
    assert event["kind"] == "group_material"
    eligible = _synth_loader.load("services.learning.evidence").EVIDENCE_ELIGIBLE_KINDS
    assert "group_material" not in eligible


# ─── 3. 入站观察分支的 fail-closed ───


def _make_plugin(tmp_path: Path, *, observe_groups: list | None = None, with_engine: bool = True):
    """构造绕过 __init__ 的插件实例（同 test_replyer_hook 口径）。"""
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    plugin._plugin_config_instance = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            mode_user_ids=["10001"],
            mode_stream_ids=[],
            timezone_offset_hours=8,
            observe_group_ids=observe_groups if observe_groups is not None else [],
            group_event_retention_days=60,
        ),
    )
    plugin._ctx = SimpleNamespace(logger=_synth_loader.null_logger())
    store = NarrativeStore(tmp_path)
    plugin._store = store
    if with_engine:
        plugin._engine = NarrativeEngine(plugin)
    plugin._group_streams = GroupStreamRegistry(store, _synth_loader.null_logger())
    plugin._local_now = lambda: datetime(2026, 9, 29, 16, 0, 0)
    plugin._group_recon_logged = True  # 测试里不打侦察 INFO
    return plugin


def test_observe_rejects_when_group_id_missing():
    """拿不到群号 → 一条都不许写（这是整条隔离链的第一道闸）。"""
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    message = _group_message()
    del message["message_info"]["group_info"]
    plugin._observe_group(message, GROUP_STREAM)
    assert plugin._store.list_events(f"group:{GID}", limit=5) == []


def test_observe_rejects_when_list_empty():
    """``observe_group_ids`` 为空 = 观察整体关闭（默认值 → 上线即安全）。"""
    plugin = _make_plugin(_tmp_dir(), observe_groups=[])
    plugin._observe_group(_group_message(), GROUP_STREAM)
    assert plugin._store.list_events(f"group:{GID}", limit=5) == []


def test_observe_rejects_group_outside_list():
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    plugin._observe_group(_group_message(gid=OTHER_GID), GROUP_STREAM)
    assert plugin._store.list_events(f"group:{OTHER_GID}", limit=5) == []


def test_observe_stores_tagged_sample_and_registers_session():
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    plugin._observe_group(_group_message("acbebbfabb"), GROUP_STREAM)
    events = plugin._store.list_events(f"group:{GID}", limit=5)
    assert len(events) == 1
    assert events[0]["source_uid"] == f"g:{GID}"
    assert events[0]["kind"] == "group_material"
    assert "acbebbfabb" in events[0]["bysource"]
    # replyer hook 没有群字段 → 靠这张映射把 session 认回群
    assert plugin._group_streams.gid_of(GROUP_STREAM) == GID


def test_observe_skips_command_and_notify():
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    plugin._observe_group(_group_message("/narrative status"), GROUP_STREAM)
    notify = _group_message("拍了拍你")
    notify["is_notify"] = True
    plugin._observe_group(notify, GROUP_STREAM)
    assert plugin._store.list_events(f"group:{GID}", limit=5) == []


def test_observe_does_not_touch_private_state():
    """群聊**零状态写入**：不写支线事件、不动互动计数、不污染私聊会话映射。"""
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    before_kv = dict(plugin._store.get_kv("stream_map") or {})
    plugin._observe_group(_group_message("群里聊天"), GROUP_STREAM)
    # ① 群素材一条都没进私聊支线队列（这是"群聊绝不污染私聊"最直接的证据）
    assert plugin._store.list_events("branch:2111957354", limit=10) == []
    # ② 私聊会话映射没被污染（群 session 走的是另一张表）
    assert dict(plugin._store.get_kv("stream_map") or {}) == before_kv
    # ③ 互动计数没被累加（Q2=B：群聊不做支线反馈）
    branch = plugin._engine.load_branch_state("2111957354")
    assert int(branch.get("state", {}).get("interaction_count", 0)) == 0
    # ④ 自我层互动时点没被更新（字段默认存在，看的是**有没有被写上值**）
    state = plugin._engine.load_self_state()
    assert not state["state"].get("last_interaction_ts")


def test_inbound_hook_never_aborts_group_message():
    """群聊无论发生什么都必须 continue —— 回复判定全归宿主 reply_necessity。"""
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    plugin._streams = SimpleNamespace(uid_of=lambda sid: "")
    plugin._telemetry = SimpleNamespace()
    kwargs = {"message": _group_message(), "stream_id": GROUP_STREAM}
    result = asyncio.run(plugin.handle_inbound_message(**kwargs))
    assert result["action"] == "continue"
    assert result["modified_kwargs"] == kwargs


# ─── 4. 保留窗口（60 天） ───


def test_expired_group_events_are_cleaned():
    """事件队列唯一的清道夫必须能扫到群；否则群事件无限增长。"""
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    now = datetime(2026, 9, 29, 16, 0, 0)
    old_ts = (now - timedelta(days=61)).isoformat(timespec="seconds")
    fresh_ts = (now - timedelta(days=10)).isoformat(timespec="seconds")
    plugin._store.push_event(
        {"ts": old_ts, "scope": f"group:{GID}", "kind": "group_material", "bysource": "旧"}
    )
    plugin._store.push_event(
        {"ts": fresh_ts, "scope": f"group:{GID}", "kind": "group_material", "bysource": "新"}
    )
    plugin._engine._dequeue_expired_group_events(now)
    left = plugin._store.list_events(f"group:{GID}", limit=10)
    assert [row["bysource"] for row in left] == ["新"]


def test_cleanup_reaches_groups_removed_from_list():
    """群被摘出名单后，历史事件仍要能被清掉（否则变永久孤儿数据）。"""
    tmp = _tmp_dir()
    plugin = _make_plugin(tmp, observe_groups=[GID])
    plugin._group_streams.record(GID, GROUP_STREAM)
    now = datetime(2026, 9, 29, 16, 0, 0)
    plugin._store.push_event(
        {
            "ts": (now - timedelta(days=90)).isoformat(timespec="seconds"),
            "scope": f"group:{GID}",
            "kind": "group_material",
            "bysource": "孤儿",
        }
    )
    # 模拟"用户把群从名单摘掉"
    plugin._plugin_config_instance.narrative.observe_group_ids = []
    plugin._engine._dequeue_expired_group_events(now)
    assert plugin._store.list_events(f"group:{GID}", limit=10) == []


# ─── 5. 群会话注册表持久化（P0 踩坑 #5 同款） ───


def test_group_registry_persists_and_restores():
    tmp = _tmp_dir()
    store = NarrativeStore(tmp)
    first = GroupStreamRegistry(store, _synth_loader.null_logger())
    first.record(GID, GROUP_STREAM)
    # 模拟重启：新实例什么都不记得，只能靠 kv 回填
    second = GroupStreamRegistry(store, _synth_loader.null_logger())
    second.restore()
    assert second.gid_of(GROUP_STREAM) == GID
    assert second.known_gids() == [GID]


# ─── 6. 群漂移层注入的边界 ───


def test_group_injection_carries_no_relation_nor_learned_style(monkeypatch):
    """群注入三不：不带关系语境、不带私聊 learning 风格、不认私聊 owner。"""
    tmp = _tmp_dir()
    plugin = _make_plugin(tmp, observe_groups=[GID])
    plugin._streams = SimpleNamespace(uid_of=lambda sid: "")
    # 群会话必须先登记过（replyer hook 不传群字段，靠入站时记的映射认群）
    plugin._group_streams.record(GID, GROUP_STREAM)
    plugin._engine = SimpleNamespace(
        load_self_state=lambda: {
            "state": {
                "mood": {"label": "轻快", "energy": 0.8},
                "routine": {"phase": "下午", "sleep_state": "awake", "woken_count": 0},
            }
        },
        load_branch_state=lambda uid: {"relationship": {"stage": "挚友"}},
    )
    # 私聊侧明明有学习成果；群聊侧必须一个字都不过去
    monkeypatch.setattr(
        _PLUGIN, "get_style_projection", lambda *a, **k: ["私聊里学到的口头禅"]
    )
    items = [{"item_type": "UserMessageItem", "meta": {"item_id": "x:1"}, "parts": []}]
    kwargs = {
        "items": items,
        "session_id": GROUP_STREAM,
        "item_schema_version": 1,
        "request_type": "reply",
    }
    result = asyncio.run(plugin.inject_drift_style(**kwargs))
    injected = [i for i in result["modified_kwargs"]["items"] if _REPLYER.is_style_item(i)]
    assert len(injected) == 1
    text = injected[0]["parts"][0]["text"]
    assert "私聊里学到的口头禅" not in text  # 🔴 跨流泄露闸门
    assert "挚友" not in text  # 关系语境对第三方不可见
    # 但调制段与文学授权仍在（这是群注入要的效果）
    assert text.strip()


def test_group_outside_list_is_not_injected():
    """已从名单摘除的群不得继续注入。"""
    plugin = _make_plugin(_tmp_dir(), observe_groups=[GID])
    plugin._streams = SimpleNamespace(uid_of=lambda sid: "")
    plugin._group_streams.record(OTHER_GID, GROUP_STREAM)
    items = [{"item_type": "UserMessageItem", "meta": {"item_id": "x:1"}, "parts": []}]
    result = asyncio.run(plugin.inject_drift_style(items=items, session_id=GROUP_STREAM))
    assert not any(_REPLYER.is_style_item(i) for i in result["modified_kwargs"]["items"])


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
