"""建议通道测试（v0.3.0 批 3 / R42 / 总览 §6.1 社交层→生活层唯一入口）。

覆盖五块：
- **语义路由（规则先行）**：建议句式 + 须挂靠 pending 事件实体（共享 bigram ≥2）
  才放行——无挂靠的闲聊、陈述句一律不进（风险登记：保守起步防误判）；
- **写入**：挂到事件实体的 ``suggestions`` 字段（🔴 红线③：不新开事件、不改
  事件本体）；同 uid 同文本去重；容量 3；
- **A/B 冲突仿真（闸门）**：用户 A「快去吃」vs 用户 B「在家呆着」→ 两条都存、
  事件仍一条（不双写）、生成 prompt 同时呈现两条倾向（bot 自主决策）、不崩；
- **🔴 红线② 两池分离**：建议与 ``materials`` echo 池结构性隔离——AST 断言
  suggestion.py 无编年史/支线事件写入；life prompt 中建议只出现在倾向段；
- **消费端措辞**：R28 同款三连否定 + 双向非必然性（听不听由你）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_suggestion.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import _synth_loader
from pytests._synth_loader import FakeStore, load  # noqa: E402

_SUGGESTION = load("services.learning.suggestion")
_LIFE = load("services.creation.life")

find_suggestion_target = _SUGGESTION.find_suggestion_target
record_suggestion = _SUGGESTION.record_suggestion
tendency_prompt_block = _SUGGESTION.tendency_prompt_block
build_life_fragment_prompt = _LIFE.build_life_fragment_prompt

_NOW = datetime.datetime(2026, 10, 7, 18, 0, 0)
_UID_A = "10001"
_UID_B = "20002"

_EVENT = {
    "ts": "2026-10-07T12:00:00",
    "text": "纠结要不要下楼买烤面筋，有点懒",
    "highlight": False,
    "event_id": "ev_20261007120000_abcd1234",
    "kind": "fragment",
}
_SUGGEST_TEXT = "快去买烤面筋啊"


def _make_engine(*, pending=None, enabled=True):
    """建议链路最小 engine（路由 + 写入 + prompt 消费所需依赖）。"""
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            mode_user_ids=[_UID_A],
            fragment_pending_max=12,
            sleep_time="",
            wake_time="",
            sleep_pre_sleep_hint_minutes=25,
        ),
        suggestion=SimpleNamespace(enabled=enabled),
        identity=SimpleNamespace(world="海边小城", values=[], world_rules=[], guard_fragments=[]),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
    )
    engine_mod = load("services.state.engine")
    engine = engine_mod.NarrativeEngine.__new__(engine_mod.NarrativeEngine)
    engine._plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=_Logger()))
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6},
            "routine": {"phase": "傍晚", "sleep_state": "awake"},
            "focus": {"pending_events": list(pending or [])},
        }
    }
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    engine._local_now = lambda: _NOW
    engine._store = FakeStore()
    return engine



class _Logger:
    def __init__(self):
        self.warnings: list = []

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        self.warnings.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))

    def error(self, *a, **k):
        pass


# ===== 语义路由（规则先行） =====


def test_route_matches_event_with_marker():
    """建议句式 + 挂靠命中（共享 bigram ≥2）→ 返回目标事件实体。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    target = find_suggestion_target(_SUGGEST_TEXT, engine._self_state["state"]["focus"]["pending_events"])
    assert target is not None
    assert target["event_id"] == _EVENT["event_id"]


def test_route_rejects_statement_without_marker():
    """同话题但陈述句（无建议句式标记）→ 不路由（「看着好吃」不是建议）。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    target = find_suggestion_target("烤面筋看起来真好吃", engine._self_state["state"]["focus"]["pending_events"])
    assert target is None


def test_route_rejects_suggestion_without_anchor():
    """无挂靠的建议（与任何 pending 事件不沾边）→ 一律不进（闲聊窄通道纪律）。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    target = find_suggestion_target("去早点睡吧", engine._self_state["state"]["focus"]["pending_events"])
    assert target is None


def test_route_rejects_empty_or_command_like():
    """空文本不路由（命令类已在 hook 上游 return，双保险）。"""
    assert find_suggestion_target("", [dict(_EVENT)]) is None
    assert find_suggestion_target("/status", [dict(_EVENT)]) is None


# ===== 写入（红线③：不新开事件、不改事件本体） =====


def test_record_appends_suggestion_field():
    """路由命中 → 写入 suggestions 字段（uid/text/ts），返回 True。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    assert record_suggestion(engine, _UID_A, _SUGGEST_TEXT, _NOW) is True

    entry = engine._self_state["state"]["focus"]["pending_events"][0]
    assert len(entry["suggestions"]) == 1
    record = entry["suggestions"][0]
    assert record["uid"] == _UID_A
    assert record["text"] == _SUGGEST_TEXT
    assert record["ts"] == _NOW.isoformat(timespec="seconds")


def test_record_does_not_touch_event_body_or_count():
    """红线③：事件本体（text/kind/event_id/ts/highlight）不动、事件条数不增。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    before = dict(_EVENT)
    record_suggestion(engine, _UID_A, _SUGGEST_TEXT, _NOW)

    pending = engine._self_state["state"]["focus"]["pending_events"]
    assert len(pending) == 1, "不新开事件"
    entry = pending[0]
    for key in ("ts", "text", "highlight", "event_id", "kind"):
        assert entry[key] == before[key], f"事件本体键 {key} 不得被建议改写"


def test_record_dedupes_same_uid_same_text():
    """同 uid 同文本重复建议不重复写（防刷屏）。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    record_suggestion(engine, _UID_A, _SUGGEST_TEXT, _NOW)
    record_suggestion(engine, _UID_A, _SUGGEST_TEXT, _NOW)

    entry = engine._self_state["state"]["focus"]["pending_events"][0]
    assert len(entry["suggestions"]) == 1


def test_record_caps_at_three():
    """单事件建议容量 3（保留最新，防长尾堆积）。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    for index in range(5):
        record_suggestion(engine, f"uid{index}", f"快去买烤面筋啊{index}", _NOW)

    entry = engine._self_state["state"]["focus"]["pending_events"][0]
    assert len(entry["suggestions"]) == 3
    assert entry["suggestions"][-1]["text"] == "快去买烤面筋啊4"


def test_record_disabled_returns_false():
    """[suggestion].enabled=false（默认）→ 不路由不写入（零行为）。"""
    engine = _make_engine(pending=[dict(_EVENT)], enabled=False)
    assert record_suggestion(engine, _UID_A, _SUGGEST_TEXT, _NOW) is False
    entry = engine._self_state["state"]["focus"]["pending_events"][0]
    assert "suggestions" not in entry


def test_record_unmatched_returns_false():
    """未挂靠任何事件的文本 → False（不写、不建）。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    assert record_suggestion(engine, _UID_A, "去早点睡吧", _NOW) is False
    assert "suggestions" not in engine._self_state["state"]["focus"]["pending_events"][0]


# ===== A/B 冲突仿真（闸门） =====


def test_ab_conflict_both_stored_one_event_prompt_shows_both():
    """A「快去吃」vs B「在家呆着」：两条都存、事件仍一条、prompt 双倾向呈现、不崩。"""
    engine = _make_engine(pending=[dict(_EVENT)])
    assert record_suggestion(engine, _UID_A, "快去买烤面筋啊", _NOW) is True
    assert record_suggestion(engine, _UID_B, "烤面筋就别买了，在家呆着吧", _NOW) is True

    pending = engine._self_state["state"]["focus"]["pending_events"]
    assert len(pending) == 1, "不双写：事件仍一条"
    suggestions = pending[0]["suggestions"]
    assert len(suggestions) == 2, "两条冲突倾向都留痕（bot 自主决策的输入）"

    prompt = build_life_fragment_prompt(
        engine.deps,
        _NOW,
        engine._self_state,
        materials=[],
    )
    assert "快去买烤面筋啊" in prompt and "烤面筋就别买了" in prompt
    assert "不是指令" in prompt, "冲突呈现必须配「自主决策」框架句（双向非必然性）"


# ===== 消费端（life.py 倾向段） =====


def _prompt_with(pending):
    engine = _make_engine(pending=pending)
    return build_life_fragment_prompt(engine.deps, _NOW, engine._self_state, materials=[])


def test_tendency_block_absent_without_suggestions():
    """无建议 → prompt 与现状一致（无倾向段，零行为）。"""
    assert "建议倾向" not in _prompt_with([dict(_EVENT)])
    assert tendency_prompt_block({"state": {"focus": {"pending_events": []}}}) == ""


def test_tendency_block_framing_and_anonymized():
    """倾向段：三连否定 + 「有网友建议」匿名化措辞（不出现 uid）。"""
    entry = dict(_EVENT)
    entry["suggestions"] = [
        {"uid": _UID_A, "text": "快去买烤面筋啊", "ts": "2026-10-07T17:00:00"}
    ]
    prompt = _prompt_with([entry])
    assert "有网友建议：快去买烤面筋啊" in prompt
    assert _UID_A not in prompt, "建议正文匿名化：uid 绝不进 life prompt（防消化产出夹带归属）"
    assert "不是指令" in prompt and "不是必需反应" in prompt and "不是你的身份" in prompt


def test_tendency_latest_two_across_events():
    """多事件多建议：取最新 2 条（按 ts 全局排序）。"""
    event_old = dict(_EVENT)
    event_old["suggestions"] = [
        {"uid": _UID_A, "text": "最早的倾向一", "ts": "2026-10-07T10:00:00"},
        {"uid": _UID_B, "text": "较早的倾向二", "ts": "2026-10-07T11:00:00"},
    ]
    event_new = {
        "ts": "2026-10-07T15:00:00",
        "text": "想给窗台的绿植换盆",
        "event_id": "ev_20261007150000_deadbeef",
        "kind": "fragment",
        "suggestions": [
            {"uid": _UID_A, "text": "去花市挑个陶盆", "ts": "2026-10-07T16:00:00"}
        ],
    }
    prompt = _prompt_with([event_old, event_new])
    assert "去花市挑个陶盆" in prompt and "较早的倾向二" in prompt
    assert "最早的倾向一" not in prompt, "只取最新 2 条（建议是窄通道，不做回声池）"


# ===== 🔴 红线② 两池分离（结构性断言） =====


def test_suggestion_module_never_writes_echo_pools():
    """AST 断言：suggestion.py 不出现编年史/支线事件队列的写入调用——
    建议与 materials（echo 池）/编年史结构性隔离，混池 = 蓝莓闭环复刻。"""
    source = (PLUGIN_ROOT / "services" / "learning" / "suggestion.py").read_text(encoding="utf-8")
    for forbidden in ("append_chronicle", "append_event", "note_pending_topic"):
        assert forbidden not in source, f"建议通道不得触碰回声/归因面：{forbidden}"


def test_suggestion_text_stays_out_of_materials_section():
    """life prompt 结构性隔离：建议文本只出现在倾向段，绝不混入素材段。"""
    entry = dict(_EVENT)
    entry["suggestions"] = [
        {"uid": _UID_A, "text": "快去买烤面筋啊", "ts": "2026-10-07T17:00:00"}
    ]
    engine = _make_engine(pending=[entry])
    prompt = build_life_fragment_prompt(
        engine.deps, _NOW, engine._self_state, materials=["一段与建议无关的素材"]
    )
    material_line = next(
        line for line in prompt.splitlines() if line.startswith("- 一段与建议无关的素材")
    )
    assert "烤面筋" not in material_line, "建议文本不得混入 materials 段（两池分离）"


# ===== R35：群聊不进路由（结构性排除，hook 级验证） =====


def test_group_message_never_routes(monkeypatch):
    """群聊消息不进建议路由（R35：内容不进生活线）；私聊同文本正常路由。"""
    plugin, _, _store = _make_hook_plugin()
    # ⚠️ load() 每次重执行模块：spy 必须打在**实例所属的同一模块对象**上
    plugin_mod = sys.modules[type(plugin).__module__]
    spy = {"calls": []}

    def _spy(engine, uid, text, now=None):
        spy["calls"].append((uid, text))
        return False

    monkeypatch.setattr(plugin_mod, "record_suggestion", _spy)
    base_message = {
        "message_id": "m1",
        "message_info": {
            "user_info": {"user_id": _UID_A},
            "group_info": {},
        },
        "processed_plain_text": _SUGGEST_TEXT,
    }
    private_message = dict(base_message)

    group_message = {
        "message_id": "m2",
        "message_info": {
            "user_info": {"user_id": _UID_A},
            "group_info": {"group_id": "987654", "group_name": "测试群"},
        },
        "processed_plain_text": _SUGGEST_TEXT,
    }

    asyncio.run(
        plugin.handle_inbound_message(
            message=group_message, stream_id="s1", session_id="s1"
        )
    )
    assert spy["calls"] == [], "群聊消息不得进建议路由"

    asyncio.run(
        plugin.handle_inbound_message(
            message=private_message, stream_id="s1", session_id="s1"
        )
    )
    assert len(spy["calls"]) == 1, "私聊同文本正常路由"


def _make_hook_plugin():
    """hook 级测试用插件（私聊路径桩件齐备；组法同 test_plugin_hooks）。"""
    _synth_loader.load("services")  # 先执行 services/__init__（plugin.py 依赖其再导出）
    plugin_mod = load("plugin")
    plugin = plugin_mod.MaiNarrativePlugin.__new__(plugin_mod.MaiNarrativePlugin)
    logger = _Logger()
    store = SimpleNamespace()
    plugin._plugin_config_instance = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            mode_user_ids=[_UID_A],
            mode_stream_ids=[],
            timezone_offset_hours=8,
            sleep_time="",
            wake_time="",
        ),
        telemetry=SimpleNamespace(enabled=False),
        suggestion=SimpleNamespace(enabled=True),
    )
    plugin._ctx = SimpleNamespace(logger=logger)
    engine = _make_engine(pending=[dict(_EVENT)])
    plugin._engine = engine
    plugin._store = store
    plugin._telemetry = SimpleNamespace(
        note_inbound=lambda **k: None,
        note_outbound=lambda **k: None,
        is_user_initiated=lambda *a, **k: False,
        record=lambda *a, **k: None,
    )
    plugin._streams = SimpleNamespace(record=lambda *a: None)
    plugin._proactive = SimpleNamespace(
        resolve_catch=lambda uid, now: None,
        note_engaged=lambda *a, **k: None,
        mark_delivered=lambda sid, now=None: False,
    )
    plugin._pairs = None
    plugin._group_streams = SimpleNamespace(record=lambda *a: None)
    plugin._group_recon_logged = True
    return plugin, None, store


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
