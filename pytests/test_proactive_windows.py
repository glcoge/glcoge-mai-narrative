"""按用户活跃窗口「星期维度」测试（2026-09-14，测试者 3892809830 需求）。

需求原文：工作日 05:00~20:20 不接收主动开口，周末照常。

设计定案（grill 两轮）：
- 配置形态：``[proactive].user_active_windows``（裸 dict，WebUI 渲染成 ``[object Object]``，
  无法编辑）→ ``user_window_rules: List[UserWindowRule]``，字段 = QQ号 / 生效日(1-7 多选) /
  开始 / 结束。WebUI 渲染成带标签卡片行，彻底消灭 JSON 语法。
- 星期记法：ISO 数字 1-7（1=周一 … 7=周日），与 ``datetime.isoweekday()`` 对齐。
- **跨午夜按「当前时刻」的星期判定**（决策 Q3）：周五 20:20 起的窗口过了 24:00 即失效，
  不会被周六规则污染（否则与"周末照常"冲突）。
- 空 ``days`` = 该规则永不命中；某用户只有空 days 规则 → 永不主动（决策 Q4，
  修复旧版"空列表回退默认窗口"的反直觉行为）。
- 用户有规则时**完全覆盖**默认窗口，不再回退（沿用旧 per-user 覆盖语义）。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_proactive_windows.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_proactive_windows.py
"""

from __future__ import annotations

import datetime
import sys
from types import SimpleNamespace

import _synth_loader

_PROACTIVE = _synth_loader.load("services.proactive.scheduler")

rule_matches_now = _PROACTIVE.rule_matches_now
validate_rules = _PROACTIVE.validate_rules
in_windows = _PROACTIVE.in_windows

# 2026-09-14 是周一 → 用固定日期构造"今天星期几"的场景，避免测试依赖运行日期
_MONDAY = datetime.datetime(2026, 9, 14, 21, 0)      # 周一 21:00
_SATURDAY = datetime.datetime(2026, 9, 19, 21, 0)    # 周六 21:00
_SATURDAY_EARLY = datetime.datetime(2026, 9, 19, 1, 0)  # 周六 01:00（跨午夜）


def _rule(user_id="3892809830", days=None, start="20:20", end="05:00"):
    return SimpleNamespace(
        user_id=user_id,
        days=list(days) if days is not None else ["1", "2", "3", "4", "5"],
        start=start,
        end=end,
    )


# ===== 星期维度 =====


def test_rule_matches_on_listed_weekday():
    """工作日窗口 + 周一 21:00 → 命中。"""
    assert rule_matches_now(_MONDAY, _rule()) is True


def test_rule_skips_unlisted_weekday():
    """工作日窗口 + 周六 21:00 → 不命中（周末走另一条规则）。"""
    assert rule_matches_now(_SATURDAY, _rule()) is False


def test_weekend_rule_matches_on_weekend():
    """周末规则 + 周六 10:00（默认窗口内）→ 命中。"""
    saturday_morning = datetime.datetime(2026, 9, 19, 10, 0)
    weekend_rule = _rule(days=["6", "7"], start="09:00", end="22:00")
    assert rule_matches_now(saturday_morning, weekend_rule) is True


def test_cross_midnight_uses_current_weekday():
    """决策 Q3：周六 01:00 属周六，不再算作"周五晚窗口的延续"。"""
    assert rule_matches_now(_SATURDAY_EARLY, _rule(days=["1", "2", "3", "4", "5"])) is False


def test_cross_midnight_same_day_still_matches():
    """同一天内跨天窗口正常：周一 23:30 落在 20:20-05:00 内。"""
    monday_late = datetime.datetime(2026, 9, 14, 23, 30)
    assert rule_matches_now(monday_late, _rule()) is True


def test_window_end_is_exclusive():
    """区间右开：结束时刻整点不算命中（与旧 in_windows 语义一致）。"""
    monday_at_end = datetime.datetime(2026, 9, 14, 22, 0)
    assert rule_matches_now(monday_at_end, _rule(start="09:00", end="22:00")) is False


# ===== 空 days / 非法值 =====


def test_empty_days_never_matches():
    """决策 Q4：days 全不勾 = 该用户永不主动（不再回退默认窗口）。"""
    assert rule_matches_now(_MONDAY, _rule(days=[])) is False
    assert rule_matches_now(_SATURDAY, _rule(days=[])) is False


def test_invalid_time_never_matches():
    """时刻非法 → 该规则永不命中（不抛异常，不阻塞调度）。"""
    assert rule_matches_now(_MONDAY, _rule(start="25:00", end="26:00")) is False


# ===== 启动校验 =====


def test_validate_rules_accepts_valid_rule():
    """合法规则 → 无错误。"""
    assert validate_rules([_rule()]) == []


def test_validate_rules_rejects_non_numeric_qq():
    """QQ 号非纯数字 → 报错（手改 TOML 最常见的错）。"""
    errors = validate_rules([_rule(user_id="3892809830x")])
    assert len(errors) == 1
    assert "QQ" in errors[0]


def test_validate_rules_rejects_bad_weekday():
    """星期越界（如 8）→ 报错。Literal 已在 pydantic 层拦截，此处是兜底。"""
    errors = validate_rules([_rule(days=["1", "8"])])
    assert len(errors) == 1
    assert "星期" in errors[0]


def test_validate_rules_rejects_bad_time():
    """时刻非法 → 报错。"""
    errors = validate_rules([_rule(start="20:20~05:00", end="")])
    assert len(errors) == 1
    assert "时刻" in errors[0]


def test_validate_rules_reports_index():
    """多条规则时错误要带序号，方便定位是哪一行。"""
    errors = validate_rules([_rule(), _rule(user_id="bad")])
    assert len(errors) == 1
    assert "2" in errors[0]


def test_validate_rules_empty_list_ok():
    """没配任何规则 → 无错误（走默认窗口，是合法状态）。"""
    assert validate_rules([]) == []


# ===== 调度层：规则 vs 默认窗口的覆盖语义 =====


def _scheduler(rules, default=("09:00-22:00",)):
    """用最小假配置构造调度器（只依赖 config.proactive 两个字段）。"""
    cfg = SimpleNamespace(
        proactive=SimpleNamespace(
            user_window_rules=list(rules),
            default_active_window=list(default),
        )
    )
    return _PROACTIVE.ProactiveScheduler(SimpleNamespace(config=cfg))


def test_no_rules_falls_back_to_default_window():
    """没有该用户规则 → 走默认窗口（无星期维度）。"""
    sched = _scheduler([])
    assert sched._allowed_now("10001", _MONDAY.replace(hour=10)) is True
    assert sched._allowed_now("10001", _MONDAY.replace(hour=23)) is False


def test_rules_override_default_not_union():
    """有规则即**覆盖**默认窗口，不是并集（沿用旧 per-user 覆盖语义）。"""
    sched = _scheduler([_rule(user_id="10001", days=["1"], start="20:20", end="05:00")])
    assert sched._allowed_now("10001", _MONDAY.replace(hour=10)) is False
    assert sched._allowed_now("10001", _MONDAY.replace(hour=21)) is True


def test_rules_are_matched_by_user_id_strictly():
    """别人的规则不影响我；且规则用户会脱离默认窗口约束。"""
    others = _rule(user_id="99999", days=["1", "2", "3", "4", "5", "6", "7"], start="00:00", end="23:59")
    sched = _scheduler([others])
    assert sched._allowed_now("10001", _MONDAY.replace(hour=10)) is True
    assert sched._allowed_now("99999", _MONDAY.replace(hour=23, minute=30)) is True


def test_empty_days_rule_overrides_default_to_never():
    """决策 Q4：有规则但 days 全空 → 永不主动，**不**回退默认窗口。"""
    sched = _scheduler([_rule(user_id="10001", days=[])])
    assert sched._allowed_now("10001", _MONDAY.replace(hour=10)) is False
    assert sched._allowed_now("10001", _SATURDAY.replace(hour=10)) is False


def test_weekday_weekend_pair_matches_tester_need():
    """测试者 3892809830 的真实配置：工作日仅 20:20-05:00，周末 09:00-22:00。"""
    sched = _scheduler(
        [
            _rule(user_id="3892809830", days=["1", "2", "3", "4", "5"], start="20:20", end="05:00"),
            _rule(user_id="3892809830", days=["6", "7"], start="09:00", end="22:00"),
        ]
    )
    uid = "3892809830"
    assert sched._allowed_now(uid, _MONDAY.replace(hour=8)) is False      # 工作日白天：不打扰
    assert sched._allowed_now(uid, _MONDAY.replace(hour=21)) is True      # 工作日晚上：可开口
    assert sched._allowed_now(uid, _SATURDAY.replace(hour=10)) is True    # 周末白天：照常
    assert sched._allowed_now(uid, _SATURDAY.replace(hour=23)) is False   # 周末夜里：不打扰


# ===== 默认窗口仍可用（无规则用户） =====


def test_in_windows_still_supports_cross_day():
    """旧的 default_active_window（无星期维度）行为不变。"""
    assert in_windows(datetime.time(23, 30), ["20:20-05:00"]) is True
    assert in_windows(datetime.time(12, 0), ["20:20-05:00"]) is False


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
