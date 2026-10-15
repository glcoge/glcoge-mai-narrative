"""意图账本（P2 / R45 / ADR-0006）记账端测试。

覆盖：意图行解析（剥离/字段/多行取末）、due 分档封顶、commitment 实体构造、
账本登记与容量淘汰、prompt 意图行说明的位置契约。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_intent.py -q
"""

from __future__ import annotations

import datetime
import types

import _synth_loader

_intent = _synth_loader.load("services.intent")
_entity = _synth_loader.load("services.creation.event_entity")
_life = _synth_loader.load("services.creation.life")

parse_intent_line = _intent.parse_intent_line
resolve_due = _intent.resolve_due
register_commitment = _intent.register_commitment
commitment_status = _intent.commitment_status
make_commitment_event = _entity.make_commitment_event
build_life_fragment_prompt = _life.build_life_fragment_prompt
Deps = _synth_loader.load("services.deps").Deps
# intent.resolve_due 延迟导入 ..state.engine（parse_clock）——合成包按需注册，
# 本文件必须先显式加载 engine，相对导入才能在 sys.modules 里解析
_synth_loader.load("services.state.engine")

_NOW = datetime.datetime(2026, 10, 15, 18, 0, 0)


def _engine():
    """最小 Deps（prompt 构造器只读 config，同 test_comm_fact 夹具）。"""
    return Deps(
        config=types.SimpleNamespace(
            identity=types.SimpleNamespace(world="普通现代都市", values=["诚实"]),
            narrative=_synth_loader.sleep_config(sleep_time="", wake_time=""),
        ),
        store=types.SimpleNamespace(),
        logger=_synth_loader.make_logger(),
        local_now=lambda: _NOW,
    )


def _state():
    return {
        "state": {
            "mood": {"label": "平静", "energy": 0.5, "last_shift_ts": ""},
            "routine": {"phase": "夜晚", "sleep_state": "awake"},
            "focus": {"pending_events": []},
            "last_interaction_ts": "",
            "last_talk_date": "",
        }
    }


def _fragment_prompt():
    return build_life_fragment_prompt(_engine(), _NOW, _state(), ["聊到想养只猫"])


def _register_deps(pending_cap: int = 2):
    """register_commitment 的最小 deps：config（sleep/intent）+ 可捕获 save 的 state。"""
    state = {"state": {"focus": {"commitments": []}}}
    saved: list = []
    deps = types.SimpleNamespace(
        config=types.SimpleNamespace(
            narrative=types.SimpleNamespace(sleep_time="23:30"),
            intent=types.SimpleNamespace(pending_cap=pending_cap),
        ),
        state=types.SimpleNamespace(
            load_self_state=lambda: state,
            save_self_state=lambda s: saved.append(dict(s)),
        ),
        telemetry=None,
    )
    return deps, state, saved


# ─── 意图行解析 ──────────────────────────────────────────────────


def test_parse_no_intent_line_returns_original():
    text = "今天做了顿饭，有点咸。\n忽然想起阳台的花没浇。"
    body, intent = parse_intent_line(text)
    assert body == text
    assert intent is None


def test_parse_valid_line_stripped_and_fields():
    text = "有点惦记那家伙的晚饭。\n[意图] 那家伙|晚饭吃没吃|今晚"
    body, intent = parse_intent_line(text)
    assert "[意图]" not in body
    assert body == "有点惦记那家伙的晚饭。"
    assert intent == {"who": "那家伙", "what": "晚饭吃没吃", "when": "今晚"}


def test_parse_malformed_line_still_stripped():
    """字段不全 → 登记放弃，但行绝不漏进正文（元数据纪律）。"""
    text = "正文一句。\n[意图] 他|吃饭"
    body, intent = parse_intent_line(text)
    assert "[意图]" not in body
    assert intent is None


def test_parse_invalid_when_word_rejected():
    text = "正文一句。\n[意图] 他|吃饭|下周五下午三点"
    body, intent = parse_intent_line(text)
    assert "[意图]" not in body
    assert intent is None, "LLM 不估具体时刻，非法档位整行放弃"


def test_parse_multiple_lines_takes_last():
    text = "正文。\n[意图] 甲|借书|明天\n[意图] 乙|还钱|这两天"
    body, intent = parse_intent_line(text)
    assert intent == {"who": "乙", "what": "还钱", "when": "这两天"}
    assert body == "正文。"


# ─── due 分档封顶 ────────────────────────────────────────────────


def test_due_tonight_maps_to_one_hour_before_sleep():
    due = datetime.datetime.fromisoformat(resolve_due("今晚", _NOW, "23:30"))
    assert (due.year, due.month, due.day) == (_NOW.year, _NOW.month, _NOW.day)
    assert due.hour == 22 and due.minute == 30


def test_due_tonight_clamps_when_sleep_window_passed():
    late = _NOW.replace(hour=23, minute=0)
    due = datetime.datetime.fromisoformat(resolve_due("今晚", late, "23:30"))
    assert due == late + datetime.timedelta(hours=1), "睡前1h已过 → 至少推1小时"


def test_due_tonight_falls_back_without_sleep_time():
    due = datetime.datetime.fromisoformat(resolve_due("今晚", _NOW, ""))
    assert due == _NOW + datetime.timedelta(hours=3)


def test_due_tomorrow_maps_to_next_day_afternoon():
    due = datetime.datetime.fromisoformat(resolve_due("明天", _NOW, "23:30"))
    assert due == datetime.datetime(2026, 10, 16, 14, 0, 0)


def test_due_two_days_maps_to_plus_48h():
    due = datetime.datetime.fromisoformat(resolve_due("这两天", _NOW, "23:30"))
    assert due == _NOW + datetime.timedelta(hours=48)


# ─── commitment 实体 ─────────────────────────────────────────────


def test_commitment_event_shape_and_idempotent_id():
    a = make_commitment_event("2026-10-15T18:00:00", "一直惦记着他：吃饭", "吃饭", "他", "2026-10-15T22:30:00")
    b = make_commitment_event("2026-10-15T18:00:00", "一直惦记着他：吃饭", "吃饭", "他", "2026-10-15T22:30:00")
    assert a["kind"] == "commitment"
    assert a["event_id"] == b["event_id"], "同输入恒同 id（重放幂等）"
    assert a["highlight"] is False
    assert "status" not in a, "缺键 = pending（schema 表口径）"
    assert a["target_ref"] == "他" and a["due_ts"].endswith(":00")


# ─── 账本登记与容量淘汰 ──────────────────────────────────────────


def test_register_appends_and_saves():
    deps, state, saved = _register_deps()
    ok = register_commitment(
        deps, state, now=_NOW, who="那家伙", what="晚饭吃没吃", when="今晚"
    )
    assert ok is True
    book = state["state"]["focus"]["commitments"]
    assert len(book) == 1
    entry = book[0]
    assert entry["kind"] == "commitment"
    assert entry["target_ref"] == "那家伙"
    assert commitment_status(entry) == "pending"
    assert entry["text"] == "一直惦记着那家伙：晚饭吃没吃"
    assert saved, "登记后必须 save_self_state"


def test_register_cap_overflow_expires_oldest():
    deps, state, _ = _register_deps(pending_cap=2)
    for i, when in enumerate(("今晚", "明天", "这两天")):
        register_commitment(
            deps, state, now=_NOW + datetime.timedelta(hours=i), who=f"用户{i}", what="事", when=when
        )
    book = state["state"]["focus"]["commitments"]
    assert len(book) == 3
    statuses = [(e["target_ref"], commitment_status(e), e.get("verdict")) for e in book]
    assert statuses[0] == ("用户0", "expired", "stale"), "最老的 pending 被容量淘汰"
    assert statuses[1][1] == "pending" and statuses[2][1] == "pending"


def test_register_rejects_degenerate_input():
    deps, state, _ = _register_deps()
    assert register_commitment(deps, state, now=_NOW, who="", what="事", when="今晚") is False
    assert register_commitment(deps, state, now=_NOW, who="他", what="  ", when="今晚") is False
    assert state["state"]["focus"]["commitments"] == []


# ─── prompt 契约 ─────────────────────────────────────────────────


def test_fragment_prompt_has_intent_instruction():
    prompt = _fragment_prompt()
    assert "[意图]" in prompt, "意图行格式说明必须在 prompt 里"
    assert "三选一" in prompt
    assert "不要为了写而编" in prompt, "可选指令必须防编造"


def test_intent_instruction_after_output_instruction():
    """意图行说明是输出格式指令，必须排在「请以第一人称写…」之后（否则被当正文要求）。"""
    prompt = _fragment_prompt()
    assert prompt.index("请以第一人称") < prompt.index("[意图]")


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
