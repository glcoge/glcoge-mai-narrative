"""意图账本（P2 / R45 / ADR-0006）记账端测试。

覆盖：意图行解析（剥离/字段/多行取末）、due 分档封顶、commitment 实体构造、
账本登记与容量淘汰、prompt 意图行说明的位置契约。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_intent.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import sys
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


# ─── 到期重判（ADR-0006：到期 ≠ 执行）────────────────────────────

parse_verdict = _intent.parse_verdict
build_recheck_prompt = _intent.build_recheck_prompt
settle_due = _intent.settle_due


def _commitment(due: str, *, what: str = "晚饭吃没吃", last: str = "", status: str = ""):
    entry = make_commitment_event("2026-10-15T10:00:00", f"一直惦记着那家伙：{what}", what, "那家伙", due)
    if last:
        entry["last_check_ts"] = last
    if status:
        entry["status"] = status
    return entry


def _settle_plugin(
    book,
    *,
    capability: object = None,
    verdict_raw: str = "[判定] act",
):
    """settle_due 的最小 plugin 替身。capability=None 模拟基础设施故障。"""
    state = {"state": {"focus": {"commitments": list(book)}}}
    calls = {"capability": [], "prompts": [], "chronicle": []}

    async def fake_capability(name, **kwargs):
        calls["capability"].append((name, kwargs))
        if capability is None:
            raise RuntimeError("capability down")
        return capability

    async def fake_generate(prompt):
        calls["prompts"].append(prompt)
        return verdict_raw

    def fake_append_chronicle(scope, kind, text, ts=None, **kwargs):
        calls["chronicle"].append((scope, kind, text))

    cfg = types.SimpleNamespace(
        narrative=types.SimpleNamespace(sleep_time="23:30"),
        intent=types.SimpleNamespace(
            recheck_cooldown_minutes=45,
            pending_cap=8,
            wait_defer_hours=6,
            stale_expire_hours=48,
        ),
    )
    plugin = types.SimpleNamespace(
        _engine=types.SimpleNamespace(
            deps=types.SimpleNamespace(
                state=types.SimpleNamespace(
                    load_self_state=lambda: state,
                    save_self_state=lambda s: None,
                ),
                creator=types.SimpleNamespace(generate=fake_generate),
                store=types.SimpleNamespace(append_chronicle=fake_append_chronicle),
                config=cfg,
            )
        ),
        config=cfg,
        ctx=types.SimpleNamespace(
            call_capability=fake_capability,
            logger=_synth_loader.null_logger(),
        ),
        _telemetry=None,
    )
    return plugin, state, calls


def test_parse_verdict_four_words_and_garbage():
    assert parse_verdict("理由。\n[判定] act") == "act"
    assert parse_verdict("[判定] wait") == "wait"
    assert parse_verdict("[判定] fulfilled 因为对方说吃过了") == "fulfilled"
    assert parse_verdict("开头一句 [判定] cancel 尾注") == "cancel"
    assert parse_verdict("没有判定行") == "wait", "缺判定行 → 保守 wait"
    assert parse_verdict("[判定] 不知道") == "wait", "非法词 → 保守 wait"
    assert parse_verdict("[判定] act\n[判定] fulfilled") == "fulfilled", "多行取最后一次"


def test_recheck_prompt_carries_entry_and_facts():
    entry = _commitment("2026-10-15T22:30:00")
    prompt = build_recheck_prompt(entry, "你知道这些知识: 他说吃过了", _NOW)
    assert "那家伙" in prompt and "晚饭吃没吃" in prompt
    assert "吃过了" in prompt
    assert "[判定] act" in prompt and "[判定] cancel" in prompt, "四态契约必须完整在 prompt"


def test_settle_empty_book_short_circuits():
    plugin, state, calls = _settle_plugin([])
    result = asyncio.run(settle_due(plugin, "u1", _NOW))
    assert result is None
    assert calls["capability"] == [] and calls["prompts"] == []


def test_settle_skips_not_due():
    plugin, state, calls = _settle_plugin(
        [_commitment("2026-10-16T14:00:00")]
    )
    result = asyncio.run(settle_due(plugin, "u1", _NOW))
    assert result is None
    assert calls["capability"] == [], "未到期的挂念不检索不裁决"


def test_settle_retrieve_failure_defers():
    """检索基础设施失败 → 宁晚勿盲催：due 后移、不裁决（2026-10-15 裁定）。"""
    entry = _commitment("2026-10-15T17:00:00")
    plugin, state, calls = _settle_plugin([entry])
    result = asyncio.run(settle_due(plugin, "u1", _NOW))
    assert result is None
    assert calls["prompts"] == [], "检索失败不得盲裁"
    assert entry["due_ts"] == (_NOW + datetime.timedelta(hours=6)).isoformat(timespec="seconds")


def test_settle_fulfilled_resolves_and_skips_byhead():
    """10-14 催饭案回归用例：对方 18:39 已报「吃过了」，检索命中 → fulfilled → 不催。"""
    entry = _commitment("2026-10-15T17:00:00")
    plugin, state, calls = _settle_plugin(
        [entry],
        capability={"success": True, "content": "你知道这些知识: 对方18:39说吃了腐竹青菜小肉丸"},
        verdict_raw="对方已经说吃过了。\n[判定] fulfilled",
    )
    result = asyncio.run(settle_due(plugin, "u1", _NOW))
    assert result is None, "已了结的挂念不得成为由头"
    assert entry["status"] == "resolved" and entry["verdict"] == "fulfilled"
    assert "腐竹" in calls["prompts"][0], "检索事实必须进入重判 prompt"


def test_settle_act_returns_commitment_byhead():
    entry = _commitment("2026-10-15T17:00:00")
    plugin, state, calls = _settle_plugin(
        [entry],
        capability={"success": True, "content": ""},
        verdict_raw="还没回音，现在问一声合适。\n[判定] act",
    )
    result = asyncio.run(settle_due(plugin, "u1", _NOW))
    assert result is not None
    assert result["origin"] == "commitment"
    assert "惦记" in result["text"], "由头形态=未定挂念（红线 2），不是行动指令"
    assert entry["due_ts"] == (_NOW + datetime.timedelta(hours=6)).isoformat(
        timespec="seconds"
    ), "act 后 due 同步后移（防下一冷却圈原样连发）"


def test_settle_wait_defers():
    entry = _commitment("2026-10-15T17:00:00")
    plugin, state, calls = _settle_plugin(
        [entry],
        capability={"success": True, "content": ""},
        verdict_raw="[判定] wait",
    )
    assert asyncio.run(settle_due(plugin, "u1", _NOW)) is None
    assert commitment_status(entry) == "pending", "wait 不改 status（缺键=pending 口径）"
    assert entry["due_ts"] == (_NOW + datetime.timedelta(hours=6)).isoformat(timespec="seconds")


def test_settle_cancel_expires_with_chronicle_trace():
    entry = _commitment("2026-10-15T17:00:00", what="明天帮我看课表")
    plugin, state, calls = _settle_plugin(
        [entry],
        capability={"success": True, "content": ""},
        verdict_raw="课表的事已经过去了。\n[判定] cancel",
    )
    assert asyncio.run(settle_due(plugin, "u1", _NOW)) is None
    assert entry["status"] == "expired" and entry["verdict"] == "cancel"
    assert calls["chronicle"], "作废必须编年史留痕（红线 4：不静默丢弃）"
    assert calls["chronicle"][0][1] == "commitment"


def test_settle_cooldown_blocks_recent_recheck():
    entry = _commitment("2026-10-15T17:00:00", last="2026-10-15T17:40:00")
    plugin, state, calls = _settle_plugin([entry])
    assert asyncio.run(settle_due(plugin, "u1", _NOW)) is None
    assert calls["capability"] == [], "冷却期内不重复裁决（成本闸）"


def test_settle_staleness_auto_expires_without_llm():
    """due 后超 stale_expire_hours 仍未消化 → 零 LLM 自动作废。"""
    entry = _commitment("2026-10-13T10:00:00")  # 56h 前
    plugin, state, calls = _settle_plugin([entry])
    assert asyncio.run(settle_due(plugin, "u1", _NOW)) is None
    assert entry["status"] == "expired" and entry["verdict"] == "stale"
    assert calls["capability"] == [] and calls["prompts"] == []


# ─── 调度钩子（_fire 优先消费 commitment 由头）──────────────────


def _fire_plugin():
    """_fire 钩子测试的最小 plugin（settle 已被补丁替换）。

    _fire 签发由头后会继续走 append_context 注入与 proactive.trigger，
    两者都给可用的假件（注入 success、触发 queued），让链路走完。
    """

    async def fake_append(stream_id, segments, **kwargs):
        return {"success": True}

    async def fake_trigger(stream_id, *, intent="", reason="", metadata=None, **kwargs):
        return {"success": True, "queued": True, "task_id": "task-1"}

    return types.SimpleNamespace(
        config=types.SimpleNamespace(
            plugin=types.SimpleNamespace(enabled=True),
            proactive=types.SimpleNamespace(reply_extension_enabled=False),
        ),
        _engine=types.SimpleNamespace(
            deps=types.SimpleNamespace(
                store=types.SimpleNamespace(
                    get_kv_int=lambda k: 0,
                    get_kv_str=lambda k: "",
                    set_kv_int=lambda k, v: None,
                    set_kv_str=lambda k, v: None,
                )
            )
        ),
        _store=types.SimpleNamespace(
            get_kv_int=lambda k: 0,
            get_kv_str=lambda k: "",
            set_kv_int=lambda k, v: None,
            set_kv_str=lambda k, v: None,
        ),
        _telemetry=types.SimpleNamespace(record=lambda *a, **k: None),
        ctx=types.SimpleNamespace(
            logger=_synth_loader.null_logger(),
            maisaka=types.SimpleNamespace(
                append_context=fake_append,
                proactive=types.SimpleNamespace(trigger=fake_trigger),
            ),
        ),
    )


def test_fire_prefers_commitment_byhead(monkeypatch):
    """settle 返回 act 由头时，本轮由头改用它且不再取普通素材（优先消费裁定）。

    ⚠️ make_scheduler 内部重载 scheduler 模块（M2 老坑）：补丁必须打在实例所属
    模块上（同 test_proactive_material_inject 先例），打在先导 load 的模块上无效。
    """
    sched = _synth_loader.make_scheduler(_fire_plugin())
    sched_mod = sys.modules[type(sched).__module__]
    fired = []

    async def fake_settle(plugin, user_id, now):
        fired.append(user_id)
        return {"text": "一直惦记着那家伙：晚饭吃没吃", "event_id": "ev_x", "origin": "commitment"}

    monkeypatch.setattr(sched_mod, "_settle_intent_due", fake_settle)
    called = {"bysource": 0}
    monkeypatch.setattr(
        sched_mod.sourcing,
        "build_bysource_detail",
        lambda deps, uid, now: called.__setitem__("bysource", called["bysource"] + 1)
        or {"text": "普通片段", "event_id": "ev_y", "origin": "fragment"},
    )
    sched.record_sent = lambda *a, **k: None
    asyncio.run(sched._fire("u1", "s1", _NOW))
    assert fired == ["u1"]
    assert called["bysource"] == 0, "act 由头在场时不得再取普通素材"


def test_fire_falls_through_to_normal_byhead(monkeypatch):
    """settle 返回 None（无/未过/已了结）→ 照常普通素材签发（零行为回归）。"""
    sched = _synth_loader.make_scheduler(_fire_plugin())
    sched_mod = sys.modules[type(sched).__module__]

    async def fake_settle(plugin, user_id, now):
        return None

    monkeypatch.setattr(sched_mod, "_settle_intent_due", fake_settle)
    monkeypatch.setattr(
        sched_mod.sourcing,
        "build_bysource_detail",
        lambda deps, uid, now: {"text": "普通片段", "event_id": "ev_y", "origin": "fragment"},
    )
    sched.record_sent = lambda *a, **k: None
    asyncio.run(sched._fire("u1", "s1", _NOW))  # 不得抛——普通链路原样


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
