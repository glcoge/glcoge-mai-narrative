"""通信事实标记（v0.2.0 批 4 · C9 / R25 / ADR-0003 §8）。

**问题**：生活片段与每日小结都是*私下独白*，模型会顺手把「想联系谁」写成
「已经给他发了消息」；下一轮生成又把这个片段当事实引用（"就我刚才说的"），
而对方毫无记忆 → 对话自相矛盾，且污染长期记忆。

**本批的解法（E8 裁定 (b)：只做合约句）**：在创作合约里明文写「通信事实以真实
记录为准，片段里的联系只能写**意图**」。注入「最近真实已发送清单」需要接宿主
账本，**留收尾里程碑**——本文件用「prompt 里没有清单标记」把这条边界钉住。

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

_NOW = datetime.datetime(2026, 9, 26, 21, 30, 0)


def _engine():
    return types.SimpleNamespace(
        _plugin=types.SimpleNamespace(
            config=types.SimpleNamespace(
                identity=types.SimpleNamespace(world="普通现代都市", values=["诚实"]),
                narrative=_synth_loader.sleep_config(sleep_time="", wake_time=""),
            )
        )
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


def _fragment_prompt(*, tier="flat", wake=False, materials=("聊到想养只猫",)):
    return build_life_fragment_prompt(
        _engine(), _NOW, _state(), list(materials), tier=tier, wake=wake
    )


# ─── 合约句本体 ─────────────────────────────────────────────────


def test_rule_declares_ledger_authority():
    """「通信事实以真实记录为准」是这条规则的立论，不得被删。"""
    assert "通信事实" in COMM_FACT_RULE
    assert "真实记录为准" in COMM_FACT_RULE


def test_rule_forbids_fact_form_and_names_intent_form():
    assert "既成事实" in COMM_FACT_RULE, "必须点名禁止的形态"
    assert "意图" in COMM_FACT_RULE or "打算" in COMM_FACT_RULE, "必须给出正确的写法"
    # 给出正向例子（模型照例子学比照禁令学稳）
    assert "想回" in COMM_FACT_RULE


def test_rule_covers_the_three_contact_verbs():
    for verb in ("发消息", "打电话", "说过什么话"):
        assert verb in COMM_FACT_RULE, f"应覆盖的联系形态缺失: {verb}"


# ─── 接入创作合约 ───────────────────────────────────────────────


def test_life_fragment_flat_has_rule():
    assert COMM_FACT_RULE in _fragment_prompt(tier="flat")


def test_life_fragment_major_has_rule():
    assert COMM_FACT_RULE in _fragment_prompt(tier="major", materials=("聊了很久",) * 5)


def test_life_fragment_wake_variant_has_rule():
    """起床补一段也要带（醒来第一件事常常就是"想起要回谁"）。"""
    assert COMM_FACT_RULE in _fragment_prompt(tier="flat", wake=True)


def test_life_fragment_without_materials_still_has_rule():
    assert COMM_FACT_RULE in _fragment_prompt(tier="minor", materials=())


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
        _fragment_prompt(tier="flat"),
        _fragment_prompt(tier="major", materials=("a",) * 5),
        build_chronicle_prompt(_engine(), _NOW, _state(), ["今天聊了猫"]),
    ]
    for prompt in prompts:
        for marker in ("已发送清单", "最近发送", "发送记录清单"):
            assert marker not in prompt, f"清单注入属收尾范围，本批不应出现: {marker}"


def test_rule_is_positioned_before_output_instruction():
    """合约句必须排在最后那条「请以第一人称写…」之前，否则会被当成正文要求。"""
    prompt = _fragment_prompt(tier="flat")
    assert prompt.index(COMM_FACT_RULE) < prompt.index("请以第一人称")


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
