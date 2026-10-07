"""主动轮注入规则测试（复读话尾修复 + 履约放行 + 时间锚点）。

背景（2026-09-11 排查）：主动消息常复读 bot 自己上次对话的句式与用词，
非活跃流尤其显著。根因三重叠加：① 主动轮指令未禁止字面复读；② 由头拼在
注入块末尾、被靠后的聊天历史淹没；③ 缺少"距上次对话多久"的时间锚点。

修复原则（用户场景确认）：**禁字面复读，但放行语义履约**——"昨天约好今天聊"
这类承接必须保留（拟真加分项），禁的只是"重复自己说过的句式和用词"。

时间锚点数据源（2026-10-06 裁定）：读 **branch 层**（per-user）互动时点，
不读 self 层全局值——全局值是"最近一次和**任何人**对话"，多用户下会把
"刚跟别人聊完"误报成"刚跟你聊完"（履约/开新头判断随之失真）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_render_turn.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_render_turn.py
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace
from typing import Optional

import _synth_loader

_synth_loader.load("services.state.engine")
_RENDER = _synth_loader.load("services.render.planner_block")

build_context_block = _RENDER.build_context_block

_NOW = datetime.datetime(2026, 9, 11, 10, 30, 0)


def _make_plugin(*, sleep_time: str = "", wake_time: str = "") -> SimpleNamespace:
    """本文件不测睡眠：默认留空关闭睡眠态（注入块因此不含睡眠提示行）。"""
    return SimpleNamespace(
        config=SimpleNamespace(
            identity=SimpleNamespace(
                world="赛博朋克沿海城市",
                values=["怕麻烦但心软"],
                world_rules=["不能透露自己是 bot"],
                immutable_traits=["银发狐妖"],
            ),
            narrative=_synth_loader.sleep_config(sleep_time=sleep_time, wake_time=wake_time),
        )
    )


def _make_state(last_interaction_ts: str = "") -> dict:
    return {
        "state": {
            "mood": {"label": "平静", "energy": 0.45, "last_shift_ts": ""},
            "routine": {"phase": "上午", "sleep_state": "awake"},
            "focus": {"pending_events": []},
            "last_interaction_ts": last_interaction_ts,
            "last_talk_date": "",
        }
    }


def _make_branch(last_interaction_ts: str = "") -> dict:
    """支线层状态（per-user，engine.load_branch_state 的默认模板形状）。

    时间锚点自 2026-10-06 起读本层 ``state.last_interaction_ts``（批 4 裁定：
    全局值是"最近一次和任何人对话"，不能拿来当"跟你的"用）。
    """
    return {
        "relationship": {"stage": "", "first_met": "", "milestones": []},
        "state": {"last_interaction_ts": last_interaction_ts, "interaction_count": 0},
        "meta": {"version": 1, "updated_ts": ""},
    }


def _render(
    *,
    round_kind: str = "reply",
    bysource: str = "",
    last_interaction_ts: str = "",
    branch: Optional[dict] = None,
) -> str:
    return build_context_block(
        _make_plugin(),
        _make_state(last_interaction_ts),
        branch,
        _NOW,
        [],
        round_kind=round_kind,
        bysource=bysource,
    )


# ===== 注入端护栏：素材池容量 ≠ 注入条数（方案 §7 / P20，2026-09-30） =====


def test_planner_block_injects_at_most_two_fragments():
    """planner 块仍只取最近 2 条片段（容量放宽只作用于取材侧）。

    素材池容量 5→12 放宽的是**由头取材**范围；planner 块是每轮对话都要塞进
    prompt 的常驻上下文，条数直接换 token，必须保持最近 2 条。本用例是护栏——
    防止将来顺手把注入端也改宽（那会让每轮成本随容量线性上涨）。
    """
    state = _make_state()
    state["state"]["focus"]["pending_events"] = [
        {"ts": "2026-09-11T08:00:00", "text": "最早的片段"},
        {"ts": "2026-09-11T09:00:00", "text": "中间的片段"},
        {"ts": "2026-09-11T10:00:00", "text": "最新的片段"},
    ]
    text = build_context_block(_make_plugin(), state, None, _NOW, [])
    assert "最新的片段" in text and "中间的片段" in text
    assert "最早的片段" not in text, "注入端只应取最近 2 条（容量放宽不改变注入条数）"


# ===== 回归用例 =====


def test_proactive_hint_forbids_verbatim_echo():
    """主动轮指令必须明确禁止字面复读（句式/用词不得重复、不得改写式复述）。"""
    text = _render(round_kind="proactive", bysource="刚做了个梦被人喊名字")

    assert "不要重复" in text, "主动轮指令缺'不要重复'约束"
    assert "句式" in text and "用词" in text, "禁复读约束需点名'句式'与'用词'"


def test_proactive_hint_allows_commitment_followup():
    """主动轮指令必须放行语义履约（约定/未竟话题的承接），不能一刀切禁接上文。"""
    text = _render(round_kind="proactive", bysource="刚做了个梦被人喊名字")

    assert "约定" in text or "没说完" in text, "主动轮指令需放行履约承接"
    assert "新的" in text, "履约必须带新内容/新事由"


def test_proactive_bysource_before_hint():
    """由头必须出现在禁复读规则之前（避免被靠后的聊天历史淹没）。"""
    bysource = "刚做了个梦被人喊名字醒了才发现风吹窗帘"
    text = _render(round_kind="proactive", bysource=bysource)

    assert bysource in text, "由头文本未注入"
    assert text.index(bysource) < text.index("不要重复"), "由头应排在禁复读规则之前"


def test_proactive_time_anchor_rendered():
    """时间锚点：距上次对话 20 小时 → 注入'20 小时'提示（读 branch 层时点）。"""
    last_ts = (_NOW - datetime.timedelta(hours=20)).isoformat(timespec="seconds")
    text = _render(
        round_kind="proactive",
        bysource="刚做了个梦",
        branch=_make_branch(last_interaction_ts=last_ts),
    )

    assert "20 小时" in text, "缺少'距上次对话 20 小时'时间锚点"
    assert "距离上次对话" in text


def test_proactive_time_anchor_skipped_when_fresh():
    """刚聊完（<1 小时）不注入时间锚点（避免噪声），且不因缺时间戳报错。"""
    fresh_ts = (_NOW - datetime.timedelta(minutes=20)).isoformat(timespec="seconds")
    text = _render(
        round_kind="proactive",
        bysource="刚做了个梦",
        branch=_make_branch(last_interaction_ts=fresh_ts),
    )

    assert "距离上次对话" not in text
    assert "不要重复" in text, "无锚点时主动轮规则仍须生效"

    # branch 层时间戳缺失/损坏也不应抛异常
    text_empty = _render(
        round_kind="proactive", bysource="刚做了个梦", branch=_make_branch()
    )
    assert "不要重复" in text_empty


def test_proactive_time_anchor_reads_branch_not_global():
    """锚点必须读 branch 层时点，不得用 self 层全局值冒充（跨用户误报护栏）。

    场景：bot 20 小时前刚跟**别人**聊过（self 层全局值），但跟**这位**用户
    5 小时前聊过——对这位用户的主动轮应该说"过去约 5 小时"，而不是
    "隔了一夜或更久"（那是拿别人的互动记录冒充跟你的）。
    """
    global_ts = (_NOW - datetime.timedelta(hours=20)).isoformat(timespec="seconds")
    branch_ts = (_NOW - datetime.timedelta(hours=5)).isoformat(timespec="seconds")
    text = _render(
        round_kind="proactive",
        bysource="刚做了个梦",
        last_interaction_ts=global_ts,
        branch=_make_branch(last_interaction_ts=branch_ts),
    )

    assert "5 小时" in text, "锚点应读 branch 层（跟这位用户 5 小时前聊过）"
    assert "20 小时" not in text, "锚点不得读 self 层全局值（20 小时前是跟别人的）"


def test_proactive_time_anchor_hidden_when_no_branch_record():
    """branch 缺失或无该用户互动记录 → 锚点行不显示（fail-closed，不回退全局值）。

    宁可不说"距上次对话多久"，也不能拿"跟别人的互动"冒充"跟你的"——
    回退到全局值会把首次主动开口误报成"我们隔了一夜没聊"。
    """
    global_ts = (_NOW - datetime.timedelta(hours=20)).isoformat(timespec="seconds")

    # branch 层存在但无互动记录（新用户默认模板）
    text_new = _render(
        round_kind="proactive",
        bysource="刚做了个梦",
        last_interaction_ts=global_ts,
        branch=_make_branch(),
    )
    assert "距离上次对话" not in text_new, "无该用户互动记录时不应显示锚点"

    # branch 整个没传（非剧本会话/无 uid）同样不显示
    text_none = _render(
        round_kind="proactive",
        bysource="刚做了个梦",
        last_interaction_ts=global_ts,
        branch=None,
    )
    assert "距离上次对话" not in text_none, "branch 缺失时不应回退读 self 层全局值"


def test_reply_round_keeps_original_principles():
    """普通回应轮不注入主动轮规则（防串台）。"""
    text = _render(round_kind="reply")

    assert "不要重复" not in text
    assert "对话原则" in text


# ===== 人格类硬编码约束回退（2026-09-21，0944cae 全量回退）=====


def test_no_hardcoded_persona_constraints():
    """② 听者立场与 ⑩ 关系边界铁律已回退：人格/角色扮演类约束不写死在代码里
    （挂载不同人设会不兼容），边界约束改由 [identity].world_rules / values 承载。"""
    reply_text = _render(round_kind="reply")
    proactive_text = _render(round_kind="proactive", bysource="刚做了个梦被人喊名字")

    for text in (reply_text, proactive_text):
        assert "不主动给建议" not in text, "听者立场不应再硬编码"
        assert "关系边界" not in text, "关系边界铁律不应再硬编码"
        assert "排他性亲密" not in text, "反排他性亲密约束不应再硬编码"
        assert "愧疚" not in text, "反愧疚诱导约束不应再硬编码"


def test_state_change_still_expressed_in_words():
    """对话原则保留「状态变化要体现在话语里」（去掉"情绪和"，降低人格限制强度）。"""
    text = _render(round_kind="reply")

    assert "状态变化要体现在话语里" in text
    assert "情绪和状态变化" not in text, "不应再强制外露情绪（压抑型人设会被迫表达）"


# ===== 世界书注入（v0.3.0 批 1 / R43 / grill 定案 2026-09-05） =====
#
# 零行为 diff 双保险：enabled=false（默认）或空世界书 → 注入块与现状一致；
# detailed 模式接管世界观（world 行让位给世界书），values/world_rules 铁律两种
# 模式都注入、逻辑不动（单一事实源，防双世界观并置——v0.1.2 同款教训）。
# lorebook 段不过守卫：条目是用户手写声明，与锚定层同级信任（过闸 = 关键词
# 命中自删，锚定层行同理由）。

_LOADER = _synth_loader.load("services.lorebook.loader")

_BOOK = (
    '[[entries]]\n'
    'name = "临海市"\n'
    'keys = ["临海"]\n'
    'content = "一座常年起海雾的沿海城市。"\n'
    'kind = "world"\n'
    'constant = true\n'
    'priority = "high"\n'
    '\n'
    '[[entries]]\n'
    'name = "面馆老板娘"\n'
    'keys = ["面馆"]\n'
    'content = "巷口面馆的老板娘认得每个常客。"\n'
    'kind = "cast"\n'
)


def _make_lorebook_plugin(tmp_path, *, toml_text=_BOOK, enabled=True, mode="detailed"):
    """带世界书 loader 的 plugin（loader 直接挂实例属性，与 on_load 生产装配同形）。"""
    plugin = _make_plugin()
    plugin.config.lorebook = _synth_loader.lorebook_config(enabled=enabled, mode=mode)
    path = tmp_path / "lorebook.toml"
    path.write_text(toml_text, encoding="utf-8")
    plugin._lorebook = _LOADER.LorebookLoader(path, budget=800, max_entries=100)
    return plugin


def test_no_lorebook_attr_renders_unchanged():
    """零行为 diff（夹具缺段形态）：plugin 无 lorebook 属性 → 无注入段、world 行照旧。"""
    plugin = _make_plugin()
    text = build_context_block(plugin, _make_state(), None, _NOW, [])
    assert "世界观·相关设定" not in text
    assert "你生活在：赛博朋克沿海城市" in text


def test_lorebook_enabled_empty_book_zero_diff(tmp_path):
    """零行为 diff（空书形态）：enabled=true 但世界书为空 → 注入块与现状一致。"""
    plugin = _make_lorebook_plugin(tmp_path, toml_text="", mode="simple")
    text = build_context_block(plugin, _make_state(), None, _NOW, [])
    assert "世界观·相关设定" not in text
    assert "你生活在：赛博朋克沿海城市" in text


def test_simple_mode_keeps_world_line(tmp_path):
    """simple 模式（默认）：世界书不接管，world 行照旧注入。"""
    plugin = _make_lorebook_plugin(tmp_path, mode="simple")
    text = build_context_block(plugin, _make_state(), None, _NOW, [], dialogue_text="去面馆吃碗面")
    assert "你生活在：赛博朋克沿海城市" in text


def test_detailed_mode_world_line_yields_to_book(tmp_path):
    """detailed 模式：world 行不再注入（世界观由世界书接管），铁律行不动。"""
    plugin = _make_lorebook_plugin(tmp_path, mode="detailed")
    text = build_context_block(plugin, _make_state(), None, _NOW, [], dialogue_text="")
    assert "你生活在" not in text, "detailed 下 [identity].world 不注入（单一事实源）"
    assert "价值观底线" in text and "世界观规则" in text, "values/world_rules 两模式都注入"


def test_detailed_mode_injects_matched_entries(tmp_path):
    """detailed + 触发文本：常驻条目恒进、触发词命中的条目进，未命中不进。"""
    plugin = _make_lorebook_plugin(tmp_path, mode="detailed")
    text = build_context_block(
        plugin, _make_state(), None, _NOW, [], dialogue_text="今晚想去面馆看看"
    )
    assert "世界观·相关设定" in text
    assert "一座常年起海雾的沿海城市" in text, "constant 条目不需要触发词"
    assert "巷口面馆的老板娘" in text, "keys 子串命中"
    assert "旧书店" not in text


def test_detailed_mode_no_dialogue_only_constants(tmp_path):
    """detailed + 无触发文本：只注常驻条目（本轮无对话可扫时也不断供）。"""
    plugin = _make_lorebook_plugin(tmp_path, mode="detailed")
    text = build_context_block(plugin, _make_state(), None, _NOW, [], dialogue_text="")
    assert "一座常年起海雾的沿海城市" in text
    assert "巷口面馆的老板娘" not in text


def test_lorebook_section_bypasses_guard(tmp_path):
    """lorebook 段不过守卫：条目内容含守卫关键词也不自删（锚定层同级信任）。"""
    plugin = _make_lorebook_plugin(tmp_path, mode="detailed")
    # _make_plugin 的 world_rules=["不能透露自己是 bot"] → 守卫关键词含「bot」相关碎片，
    # 常驻条目内容若过闸会被关键词命中自删；本用例钉住「不过守卫」裁定。
    text = build_context_block(plugin, _make_state(), None, _NOW, [], dialogue_text="")
    assert "一座常年起海雾的沿海城市" in text, "世界书条目是用户手写声明，不过守卫"


def test_items_dialogue_text_joins_recent_non_injected():
    """触发扫描输入提取：跳过本插件注入的 item，取最近若干轮的文本 parts。"""
    items = [
        {"parts": [{"type": "text", "text": "更早的一轮"}]},
        _RENDER.build_injected_item("本插件注入的上下文，绝不能当触发文本"),
        {"parts": [{"type": "text", "text": "中间一轮"}]},
        {"parts": [{"type": "text", "text": "最近一轮甲"}]},
        {"parts": [{"type": "text", "text": "最近一轮乙"}, {"type": "image"}]},
    ]
    text = _RENDER.items_dialogue_text(items)
    assert "最近一轮甲" in text and "最近一轮乙" in text and "中间一轮" in text
    assert "本插件注入的上下文" not in text
    assert "更早的一轮" not in text, "默认只取最近 3 个非注入 item"


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
