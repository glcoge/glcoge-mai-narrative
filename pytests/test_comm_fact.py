"""通信事实标记（v0.2.0 批 4 · C9 / R25 / ADR-0003 §8；2026-10-15 P1 收紧）。

**问题**：生活片段与每日小结都是*私下独白*，模型会顺手把「想联系谁」写成
「已经给他发了消息」；下一轮生成又把这个片段当事实引用（"就我刚才说的"），
而对方毫无记忆 → 对话自相矛盾，且污染长期记忆。

**本批的解法（E8 裁定 (b)：只做合约句）**：在创作合约里明文写「通信事实以真实
记录为准」。注入「最近真实已发送清单」需要接宿主账本，**留收尾里程碑**——
本文件用「prompt 里没有清单标记」把这条边界钉住。

**2026-10-15 收紧（P1 时态纪律，催饭复读事故裁定）**：旧版允许「打算」（想回他
一句），但 10-14 晚事故素材「一会儿还是得发消息催催那家伙」恰好落在允许侧被
planner 照办成主动催饭。四条收紧（禁既成事实 / 散文停在 now / 对人动作禁预决策 /
禁自造日程）由本文件的锚定用例锁住。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_comm_fact.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_comm_fact.py
"""

from __future__ import annotations

import datetime
import types

import _synth_loader

_LIFE = _synth_loader.load("services.creation.life")
_CHRON = _synth_loader.load("services.creation.chronicle")

COMM_FACT_RULE = _LIFE.COMM_FACT_RULE
build_life_fragment_prompt = _LIFE.build_life_fragment_prompt
build_chronicle_prompt = _CHRON.build_chronicle_prompt
_Deps = _synth_loader.load("services.deps").Deps

_NOW = datetime.datetime(2026, 9, 26, 21, 30, 0)


def _engine():
    """最小 Deps（prompt 构造器只读 config，深化 B 起吃 deps）。"""
    return _Deps(
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


def _fragment_prompt(*, highlight=False, wake=False, materials=("聊到想养只猫",)):
    return build_life_fragment_prompt(
        _engine(), _NOW, _state(), list(materials), highlight=highlight, wake=wake
    )


# ─── 合约句本体 ─────────────────────────────────────────────────


def test_rule_declares_ledger_authority():
    """「通信事实以真实记录为准」是这条规则的立论，不得被删。"""
    assert "通信事实" in COMM_FACT_RULE
    assert "真实记录为准" in COMM_FACT_RULE


def test_rule_forbids_fact_form():
    """C9 核心（2026-10-15 收紧后原样保留）：点名禁止「既成事实」形态。"""
    assert "既成事实" in COMM_FACT_RULE, "必须点名禁止的形态"


def test_rule_predetermined_outreach_examples_moved_to_forbidden_side():
    """2026-10-15 收紧核心（催饭复读事故裁定）：旧版允例（想回他一句/明天问问她）
    全部移到禁止侧，且新增事故同型例句（得发消息催他）。

    10-14 晚事故素材「一会儿还是得发消息催催那家伙」落在旧版「打算」允许侧，
    被 planner 照办成主动催饭——预决策例句必须作为**禁止例**出现在规则里，
    让模型照例子学禁令（照禁令学不稳，同 C9 正向例子教训）。
    """
    for example in ("得发消息催他", "想回他一句", "明天问问她"):
        assert example in COMM_FACT_RULE, f"禁止侧预决策例句缺失: {example}"


def test_rule_allows_undecided_concern_forms():
    """允许侧只留未定挂念与疑惑——要不要行动是之后对话轮的裁决，不是创作层的。"""
    assert "惦记" in COMM_FACT_RULE, "必须给出允许侧的挂念例句"
    assert "不知道他回了没" in COMM_FACT_RULE, "必须给出允许侧的疑惑例句"


def test_rule_names_concern_as_the_legal_future_form():
    """纪律②（散文停在 now）：未来只能以挂念/期待/犹豫的形态出现。"""
    assert "散文停在" in COMM_FACT_RULE, "必须有停笔时点的明文"
    for form in ("挂念", "期待", "犹豫"):
        assert form in COMM_FACT_RULE, f"合法未来形态缺失: {form}"


def test_rule_forbids_invented_schedule():
    """纪律④（禁自造日程性未来）：事故实录「明天居然还得大扫除」进禁止例。"""
    assert "大扫除" in COMM_FACT_RULE, "必须点名自造日程的禁止例"


def test_rule_covers_the_three_contact_verbs():
    for verb in ("发消息", "打电话", "说过什么话"):
        assert verb in COMM_FACT_RULE, f"应覆盖的联系形态缺失: {verb}"


# ─── 接入创作合约 ───────────────────────────────────────────────


def test_life_fragment_has_rule():
    assert COMM_FACT_RULE in _fragment_prompt()


def test_life_fragment_highlight_has_rule():
    assert COMM_FACT_RULE in _fragment_prompt(highlight=True, materials=("聊了很久",) * 5)


def test_life_fragment_wake_variant_has_rule():
    """起床补一段也要带（醒来第一件事常常就是"想起要回谁"）。"""
    assert COMM_FACT_RULE in _fragment_prompt(wake=True)


def test_life_fragment_without_materials_still_has_rule():
    assert COMM_FACT_RULE in _fragment_prompt(materials=())


def test_chronicle_prompt_has_rule():
    prompt = build_chronicle_prompt(_engine(), _NOW, _state(), ["今天聊了猫"])
    assert COMM_FACT_RULE in prompt


# ─── 边界：E8(b) 只做合约句，不做注入清单 ───────────────────────


def test_no_sent_list_injection_yet():
    """**范围锁定**：本批不注入「最近真实已发送清单」（需接宿主账本，留收尾）。

    若将来收尾里程碑实现了清单注入，这条会红——那正是提醒：同步更新本测试与
    R25 的登记状态，而不是让「已实现」静默溜过文档。
    """
    prompts = [
        _fragment_prompt(),
        _fragment_prompt(highlight=True, materials=("a",) * 5),
        build_chronicle_prompt(_engine(), _NOW, _state(), ["今天聊了猫"]),
    ]
    for prompt in prompts:
        for marker in ("已发送清单", "最近发送", "发送记录清单"):
            assert marker not in prompt, f"清单注入属收尾范围，本批不应出现: {marker}"


def test_rule_is_positioned_before_output_instruction():
    """合约句必须排在最后那条「请以第一人称写…」之前，否则会被当成正文要求。"""
    prompt = _fragment_prompt()
    assert prompt.index(COMM_FACT_RULE) < prompt.index("请以第一人称")


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
