"""关系四维 schema + 锚定护栏常量测试（v0.2.0 批 2 · C3）。

冻结 ADR-0002 §1/§2 的**声明层**：

- 慢变区白名单只有六个受控维度，LLM 不得自由开字段
- 关系四维 trust/closeness/boundaries/stage；stage 是派生结论、**可回退**
- ``first_met`` / ``milestones`` 是**只读事实**，不参与晋升
- 人格维度（``character.traits`` 等）**明确排除**——锚定层人格归宿主
- 锚定层字段永不改变；写入路径据此排除
- 批 2~批 4 空窗期：``stage`` 由只读事实确定性推导（不依赖被删的 familiarity）

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_continuity_schema.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_continuity_schema.py
"""

from __future__ import annotations

import _synth_loader

_CONTINUITY = _synth_loader.load("services.state.continuity")

SLOW_FIELD_PATHS = _CONTINUITY.SLOW_FIELD_PATHS
SLOW_FIELD_AUDIENCE = _CONTINUITY.SLOW_FIELD_AUDIENCE
RELATIONSHIP_DIMENSIONS = _CONTINUITY.RELATIONSHIP_DIMENSIONS
RELATIONSHIP_FACT_FIELDS = _CONTINUITY.RELATIONSHIP_FACT_FIELDS
is_slow_field = _CONTINUITY.is_slow_field
is_slow_field_visible = _CONTINUITY.is_slow_field_visible
is_anchor_field = _CONTINUITY.is_anchor_field
current_relationship_stage = _CONTINUITY.current_relationship_stage
normalize_relationship = _CONTINUITY.normalize_relationship
guard_keywords = _CONTINUITY.guard_keywords


# ─── 慢变区白名单（ADR-0002 §1） ────────────────────────────────


def test_slow_whitelist_has_exactly_six_paths():
    """受控维度就是 ADR-0002 §1 列的六个，多一个都算越权。"""
    assert SLOW_FIELD_PATHS == frozenset(
        {
            "perspective.world_view",
            "perspective.life_goals",
            "relationship.trust",
            "relationship.closeness",
            "relationship.boundaries",
            "relationship.stage",
        }
    )


def test_every_whitelisted_path_has_audience_rule():
    """白名单与受众规则表必须一一对应（漏登记 = 静默不可见）。"""
    assert set(SLOW_FIELD_AUDIENCE) == set(SLOW_FIELD_PATHS)


def test_personality_dimensions_are_excluded():
    """人格维度必须被拒绝（防双人格事故从后门放回来）。"""
    assert not is_slow_field("character.traits")
    assert not is_slow_field("personality")
    assert not is_slow_field("character.traits.cheerful")


def test_unregistered_slow_field_is_rejected():
    """白名单外的字段一律拒绝（fail-closed）。"""
    assert not is_slow_field("perspective.favorite_food")
    assert not is_slow_field("")


def test_unknown_slow_field_is_not_visible():
    """未登记维度不可见（fail-closed：没登记就不猜）。"""
    assert not is_slow_field_visible("perspective.unknown", "123")
    assert not is_slow_field_visible("", "123")


def test_general_slow_field_visible_regardless_of_audience():
    """world_view / life_goals 属通用维度，对谁都可见。"""
    assert is_slow_field_visible("perspective.world_view", "")
    assert is_slow_field_visible("perspective.world_view", "123")
    assert is_slow_field_visible("perspective.life_goals", "")


def test_per_user_slow_field_requires_matching_audience():
    """关系维度按 uid 隔离：受众为空或错配即不可见。"""
    assert not is_slow_field_visible("relationship.trust", "")
    assert not is_slow_field_visible("relationship.trust", "456", owner_uid="123")
    assert is_slow_field_visible("relationship.trust", "123", owner_uid="123")


# ─── 关系四维（ADR-0002 §1/§2） ─────────────────────────────────


def test_relationship_dimensions_are_the_four_of_adr():
    assert RELATIONSHIP_DIMENSIONS == ("trust", "closeness", "boundaries", "stage")


def test_fact_fields_are_readonly_facts():
    """first_met / milestones 是事实字段，不是演化维度。"""
    assert RELATIONSHIP_FACT_FIELDS == ("first_met", "milestones")
    for fact in RELATIONSHIP_FACT_FIELDS:
        assert fact not in RELATIONSHIP_DIMENSIONS


def test_normalize_relationship_fills_missing_dimensions():
    state = {"relationship": {"trust": 0.5}}
    relationship = normalize_relationship(state)
    for dimension in RELATIONSHIP_DIMENSIONS:
        assert dimension in relationship
    assert relationship["trust"] == 0.5, "已有值不得被覆盖"
    assert relationship["stage"] == "陌生人"


def test_normalize_relationship_keeps_facts():
    state = {"relationship": {"first_met": "2026-09-01T10:00:00", "milestones": [{"id": "x"}]}}
    relationship = normalize_relationship(state)
    assert relationship["first_met"] == "2026-09-01T10:00:00"
    assert relationship["milestones"] == [{"id": "x"}]


# ─── 空窗期过渡 stage 推导（E1 选 (c)） ─────────────────────────


def test_stage_is_stranger_without_milestones():
    """无任何事实 → 陌生人（不靠 familiarity，靠事实）。"""
    assert current_relationship_stage({}) == "陌生人"
    assert current_relationship_stage({"milestones": []}) == "陌生人"


def test_stage_becomes_acquaintance_with_plain_milestone():
    """有里程碑但无 stage 类条目 → 相识。"""
    facts = {"milestones": [{"id": "first_chat", "desc": "第一次好好聊了天"}]}
    assert current_relationship_stage(facts) == "相识"


def test_stage_reads_latest_stage_milestone():
    """milestones 里有 stage: 类条目 → 取最后一条晋升到的标签。"""
    facts = {
        "milestones": [
            {"id": "stage:熟人", "desc": "从陌生人变成熟人", "stage_label": "熟人"},
            {"id": "stage:朋友", "desc": "从熟人变成朋友", "stage_label": "朋友"},
            {"id": "other", "desc": "一起看了场电影"},
        ]
    }
    assert current_relationship_stage(facts) == "朋友"


def test_stage_derivation_is_deterministic():
    """同一事实两次推导结果一致（保离线回放确定性）。"""
    facts = {"milestones": [{"id": "stage:熟人", "stage_label": "熟人"}]}
    assert current_relationship_stage(facts) == current_relationship_stage(facts)


def test_stage_ignores_malformed_input():
    """畸形输入不炸（只读事实可能来自旧库/人工编辑）。"""
    assert current_relationship_stage(None) == "陌生人"
    assert current_relationship_stage({"milestones": "不是列表"}) == "陌生人"
    assert current_relationship_stage({"milestones": [None, "字符串"]}) == "相识"


# ─── 锚定护栏（ADR-0002 §9） ────────────────────────────────────


def test_anchor_fields_are_detected():
    assert is_anchor_field("identity.world")
    assert is_anchor_field("identity.world_rules")
    assert is_anchor_field("identity.values")
    assert is_anchor_field("personality")
    assert is_anchor_field("character.traits")


def test_non_anchor_fields_are_not_flagged():
    assert not is_anchor_field("relationship.trust")
    assert not is_anchor_field("perspective.world_view")
    assert not is_anchor_field("")
    # 前缀匹配不得误伤：identityric 不是 identity.
    assert not is_anchor_field("identityric")


# ─── 守卫关键词抽取（E6 自动 + 手工） ──────────────────────────


def test_guard_keywords_from_world_rules():
    """规则原文按标点切子句抽关键词（中文无分词，保守切分防误伤）。"""
    keywords = guard_keywords(["不可说谎，不可泄露他人隐私。"], [], [])
    assert "不可说谎" in keywords
    assert "不可泄露他人隐私" in keywords


def test_guard_keywords_from_values():
    keywords = guard_keywords([], ["诚实", "善良"], [])
    assert "诚实" in keywords and "善良" in keywords


def test_guard_keywords_manual_extra():
    """[anchor].guard_keywords 手工补充原样进集合。"""
    keywords = guard_keywords([], [], ["内部代号", "真实姓名"])
    assert "内部代号" in keywords and "真实姓名" in keywords


def test_guard_keywords_skips_too_short_fragments():
    """单字子句不入选（长度下限 2，防"的""了"误伤）。"""
    keywords = guard_keywords(["我", "你好"], [], [])
    assert "我" not in keywords
    assert "你好" in keywords


def test_guard_keywords_empty_when_no_source():
    assert guard_keywords([], [], []) == set()


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
