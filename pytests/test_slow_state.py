"""慢变现值存储 + perspective 段（v0.2.0 批 4 · C1）。

锁定 ADR-0002 §1/§9 的三条硬约束：

1. **perspective 段就位**：自我层 state 带 ``world_view`` / ``life_goals`` +
   ``origin`` / ``updated_ts`` 溯源字段（schema 2 → 3），与支线 relationship 对称。
2. **只补不删**：``normalize_perspective`` 不覆盖已有值；容器类型错时整段重建
   （混合形状比重建更难排查）。
3. **写入守卫三层**：``slow_set`` 先判锚定层（PermissionError）→ 再判白名单
   （ValueError）→ 最后判具名 actor（ValueError）。顺序即优先级，互不掩盖——
   这是「锚定层永不被写」+「不得自由开字段」+「写入必须具名可审计」的可执行形式。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_slow_state.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_slow_state.py
"""

from __future__ import annotations

import pytest

import _synth_loader

_CONTINUITY = _synth_loader.load("services.state.continuity")
_ENGINE = _synth_loader.load("services.state.engine")

PERSPECTIVE_FIELDS = _CONTINUITY.PERSPECTIVE_FIELDS
SLOW_FIELD_PATHS = _CONTINUITY.SLOW_FIELD_PATHS
SLOW_WRITE_ACTORS = _CONTINUITY.SLOW_WRITE_ACTORS
normalize_perspective = _CONTINUITY.normalize_perspective
slow_get = _CONTINUITY.slow_get
slow_set = _CONTINUITY.slow_set
default_self_state = _ENGINE.default_self_state
default_branch_state = _ENGINE.default_branch_state

ANCHOR_PATHS = ("identity.world", "identity.values", "identity.world_rules", "personality")


# ─── perspective 段就位 ─────────────────────────────────────────


def test_default_self_state_has_perspective_section():
    """自我层默认 state 必须带 perspective 段（批 4 的写入落点）。"""
    perspective = default_self_state()["perspective"]
    assert perspective["world_view"] == ""
    assert perspective["life_goals"] == []
    assert perspective["origin"] == ""
    assert perspective["updated_ts"] == ""


def test_perspective_fields_match_adr():
    """PERSPECTIVE_FIELDS 与 ADR-0002 §1 的 general 维度一一对应。"""
    assert PERSPECTIVE_FIELDS == ("world_view", "life_goals")


def test_perspective_paths_are_whitelisted_and_general():
    """perspective 两条路径必须在慢变白名单内，且受众登记为 general。

    general 是「可进通用注入 + 可进 [learned] 投影」的前提；若哪天被改成
    per_user，E5 裁定（只投影 general）就会静默失效。
    """
    for field in PERSPECTIVE_FIELDS:
        path = f"perspective.{field}"
        assert path in SLOW_FIELD_PATHS
        assert _CONTINUITY.SLOW_FIELD_AUDIENCE[path] == "general"


def test_branch_state_has_no_perspective():
    """perspective 属自我层，不进支线层（单一事实源，避免两处各存一份）。"""
    assert "perspective" not in default_branch_state()


# ─── normalize_perspective：只补不删 ────────────────────────────


def test_normalize_perspective_keeps_existing_values():
    """已有值必须原样保留（只补缺失键，不覆盖）。"""
    state = {"perspective": {"world_view": "世界是大的", "life_goals": ["学游泳"]}}
    result = normalize_perspective(state)
    assert result["world_view"] == "世界是大的"
    assert result["life_goals"] == ["学游泳"]
    # 缺的溯源字段补上
    assert result["origin"] == ""
    assert result["updated_ts"] == ""


def test_normalize_perspective_creates_section():
    """缺整段时新建，不改动 state 里其它键。"""
    state = {"state": {"mood": {"label": "平静"}}}
    normalize_perspective(state)
    assert state["perspective"]["world_view"] == ""
    assert state["state"]["mood"]["label"] == "平静"


def test_normalize_perspective_rebuilds_wrong_container_type():
    """perspective 被写成非 dict（脏数据）→ 整段重建，不留下混合形状。"""
    state = {"perspective": "这不是字典"}
    result = normalize_perspective(state)
    assert isinstance(result, dict)
    assert result["world_view"] == ""
    assert state["perspective"] is result


def test_normalize_perspective_rebuilds_non_list_goals():
    """life_goals 被写成标量 → 重建为空列表（下游按列表迭代，不能留标量）。"""
    state = {"perspective": {"life_goals": "不是列表"}}
    result = normalize_perspective(state)
    assert result["life_goals"] == []


# ─── slow_get ───────────────────────────────────────────────────


def test_slow_get_reads_whitelisted_path():
    """白名单内路径正常读到值（perspective 与 relationship 两段都支持）。"""
    self_state = {"perspective": {"world_view": "她最近在想这个"}}
    branch_state = {"relationship": {"trust": 0.4}}
    assert slow_get(self_state, "perspective.world_view") == "她最近在想这个"
    assert slow_get(branch_state, "relationship.trust") == 0.4


def test_slow_get_rejects_non_whitelisted_path():
    """白名单外抛 ValueError，**不静默返回 None**（静默会让「路径写错」伪装成「还没写」）。"""
    with pytest.raises(ValueError):
        slow_get({"perspective": {}}, "perspective.favorite_food")


def test_slow_get_returns_none_for_missing_section():
    """段缺失 → None（值还没写，不是路径非法，两种语义必须区分）。"""
    assert slow_get({}, "perspective.world_view") is None


def test_slow_get_rejects_malformed_path():
    """点分形状不对（无叶子 / 三段）→ ValueError。"""
    with pytest.raises(ValueError):
        slow_get({"perspective": {}}, "perspective")
    with pytest.raises(ValueError):
        slow_get({"perspective": {}}, "perspective.a.b")


# ─── slow_set：三层守卫 ─────────────────────────────────────────


def test_slow_set_writes_and_returns_path():
    """正常写入并返回路径（便于 `state[slow_set(...)] = v` 风格调用）。"""
    state = default_self_state()
    returned = slow_set(state, "perspective.world_view", "新的看法", actor="promotion")
    assert returned == "perspective.world_view"
    assert state["perspective"]["world_view"] == "新的看法"


def test_slow_set_creates_missing_section():
    """段缺失时自建（晋升首次写入的场景）。"""
    state = {}
    slow_set(state, "relationship.trust", 0.5, actor="promotion")
    assert state["relationship"]["trust"] == 0.5


def test_slow_set_rejects_anchor_field_with_permission_error():
    """锚定层路径 → PermissionError（ADR-0002 §9：锚定层永不改变）。

    注意断言的是 PermissionError 而**不是** ValueError：先过 assert_writable，
    锚定层不该被降级成「不在白名单」这种普通错误。
    """
    state = {}
    for path in ANCHOR_PATHS:
        with pytest.raises(PermissionError):
            slow_set(state, path, "改掉她", actor="promotion")


def test_slow_set_rejects_non_whitelisted_path():
    """白名单外 → ValueError（不得自由开字段）。"""
    with pytest.raises(ValueError):
        slow_set(default_self_state(), "perspective.favorite_food", "草莓", actor="promotion")


def test_slow_set_rejects_unknown_actor():
    """未登记 actor → ValueError（匿名写入会把留痕链断在源头）。"""
    assert "anonymous" not in SLOW_WRITE_ACTORS
    with pytest.raises(ValueError):
        slow_set(default_self_state(), "perspective.world_view", "x", actor="anonymous")


def test_slow_set_accepts_all_registered_actors():
    """四个登记写入者（seed/promotion/rollback/manual）都能写。"""
    assert SLOW_WRITE_ACTORS == frozenset({"seed", "promotion", "rollback", "manual"})
    for actor in sorted(SLOW_WRITE_ACTORS):
        state = default_self_state()
        slow_set(state, "perspective.world_view", f"由 {actor} 写", actor=actor)
        assert state["perspective"]["world_view"] == f"由 {actor} 写"


# ─── 白名单与读写实现对齐（穷举） ───────────────────────────────


def test_every_whitelisted_path_is_gettable_and_settable():
    """**穷举白名单六路径**：每条都必须能被 slow_get 读到、被 slow_set 写入。

    这条是「白名单加了路径但忘了打通读写」的护栏——两份清单分居不同代码段，
    不同步时最容易出现「提案能过白名单、写值却抛未知路径」。
    """
    self_state = default_self_state()
    branch_state = default_branch_state()
    for path in sorted(SLOW_FIELD_PATHS):
        head = path.split(".", 1)[0]
        state = self_state if head == "perspective" else branch_state
        slow_get(state, path)  # 不抛即通过
        slow_set(state, path, "写进去", actor="manual")
        assert slow_get(state, path) == "写进去"


def test_slow_set_rejects_malformed_path():
    """点分形状不对 → ValueError。"""
    with pytest.raises(ValueError):
        slow_set({}, "perspective", "x", actor="manual")


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
