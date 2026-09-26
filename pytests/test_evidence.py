"""证据层（v0.2.0 批 4 · C2）：白名单准入 / 来源桶掩码 / 事件级正向信号。

锁定用户 2026-09-26 裁定的三条强约束与两条附加禁令：

- **强约束 1 只吃正面信号**：正向信号只有 ``user_initiated_freq`` 与
  ``proactive_replied`` 两类**事件级**记录；本层不提供互动总数读取口。
- **白名单准入**（用户纠偏）：eligible kind ∈ ``{life, daily}``，
  ``promotion`` 命中即拒（自我强化循环）、``diary`` 靠设计而非巧合挡住。
- **附加禁令 A**：融合值（``urge_factor``）不得当证据 → 正向信号读取器
  **只用行数、不读 value**（value 语义跨版本变过）。
- **附加禁令 B**：判定逻辑单实现 → 本层不重写任何窗口判定，只消费已落盘记录。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_evidence.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_evidence.py
"""

from __future__ import annotations

import pytest

import _synth_loader

_EVIDENCE = _synth_loader.load("services.learning.evidence")
_AUDIENCE = _synth_loader.load("services.render.audience")

EVIDENCE_ELIGIBLE_KINDS = _EVIDENCE.EVIDENCE_ELIGIBLE_KINDS
GENERAL_KINDS = _AUDIENCE.GENERAL_KINDS
COGNITIVE_MODES = _EVIDENCE.COGNITIVE_MODES
POSITIVE_SIGNAL_METRICS = _EVIDENCE.POSITIVE_SIGNAL_METRICS
is_evidence_eligible = _EVIDENCE.is_evidence_eligible
eligible_entries = _EVIDENCE.eligible_entries
source_bucket = _EVIDENCE.source_bucket
mask_evidence = _EVIDENCE.mask_evidence
evidence_ref = _EVIDENCE.evidence_ref
is_valid_cognitive_mode = _EVIDENCE.is_valid_cognitive_mode
positive_signal_days = _EVIDENCE.positive_signal_days
positive_signal_count = _EVIDENCE.positive_signal_count
BUCKET_GENERAL = _EVIDENCE.BUCKET_GENERAL
BUCKET_RESTRICTED = _EVIDENCE.BUCKET_RESTRICTED


class _FakeMetricsStore(_synth_loader.KvStoreMixin):
    """只提供 ``read_metrics`` 的假 store（正向信号读取器的最小依赖）。"""

    def __init__(self, metrics=None):
        self._metrics = dict(metrics or {})

    def read_metrics(self, name: str):
        return list(self._metrics.get(name, []))


def _row(kind="life", source_uid="", audience="", row_id=1, text="一条素材"):
    return {
        "id": row_id,
        "ts": "2026-09-20T10:00:00",
        "scope": "self",
        "kind": kind,
        "text": text,
        "source_uid": source_uid,
        "audience": audience,
    }


def _metric(ts, user_id, value="1.0"):
    return {"ts": ts, "scope": "", "user_id": user_id, "value": value}


# ─── 白名单准入 ─────────────────────────────────────────────────


def test_eligible_kinds_equal_general_kinds():
    """**同步断言**：证据白名单与 ``render/audience.GENERAL_KINDS`` 必须一致。

    两者语义不同（那边管"可见性"，这边管"可作证据"），当前取值相同是刻意的。
    若将来有人改了 audience 的口径，本测试立刻红——逼人就「证据准入是否跟着变」
    做一次显式决定，而不是让两处静默分叉。
    """
    assert EVIDENCE_ELIGIBLE_KINDS == GENERAL_KINDS
    assert EVIDENCE_ELIGIBLE_KINDS == frozenset({"life", "daily"})


@pytest.mark.parametrize("kind", ["life", "daily"])
def test_eligible_kinds_pass(kind):
    assert is_evidence_eligible(_row(kind=kind))


@pytest.mark.parametrize("kind", ["diary", "promotion", "milestone", "", "unknown"])
def test_ineligible_kinds_rejected(kind):
    """白名单外一律拒——**含 `promotion`**（自我强化循环）与 `diary`（旧口径靠巧合）。"""
    assert not is_evidence_eligible(_row(kind=kind))


def test_promotion_trace_is_rejected_even_with_empty_source():
    """关键回归：`promotion` 留痕即使用「source_uid 空」也**不得准入**。

    这正是 v1 黑名单口径的漏洞：它只看 source_uid，会把晋升历史（模板/AI 输出）
    当成晋升证据 → 自我强化循环。白名单口径下与 source_uid 无关。
    """
    assert not is_evidence_eligible(_row(kind="promotion", source_uid=""))


def test_eligible_entries_filters_kinds():
    rows = [_row(kind="life"), _row(kind="promotion"), _row(kind="daily"), _row(kind="diary")]
    assert [entry["kind"] for entry in eligible_entries(rows)] == ["life", "daily"]


# ─── 来源桶掩码 ─────────────────────────────────────────────────


def test_source_bucket_general_and_restricted():
    assert source_bucket(_row(source_uid="")) == BUCKET_GENERAL
    assert source_bucket(_row(source_uid="927386371")) == BUCKET_RESTRICTED


def test_mask_evidence_drops_restricted_for_general_target():
    """general 目标（perspective）**代码级丢弃**带 source_uid 的条目（HDSI 5.1）。"""
    rows = [_row(kind="life", source_uid=""), _row(kind="life", source_uid="927386371")]
    kept = mask_evidence(rows, target="perspective.world_view")
    assert len(kept) == 1
    assert kept[0]["source_uid"] == ""


def test_mask_evidence_applies_whitelist_first():
    """两层过滤并存：白名单先过，`promotion`（source_uid 空）仍进不来。"""
    rows = [_row(kind="promotion", source_uid=""), _row(kind="life", source_uid="")]
    kept = mask_evidence(rows, target="perspective.life_goals")
    assert [entry["kind"] for entry in kept] == ["life"]


def test_mask_evidence_keeps_restricted_for_per_user_target():
    """per_user 目标（relationship）不吃 general 掩码——受限条目对它反而有资格。

    这说明掩码是**按受众规则**做的（取自 ``SLOW_FIELD_AUDIENCE``），
    不是无条件砍掉所有带来源的条目。
    """
    rows = [_row(kind="life", source_uid="927386371")]
    kept = mask_evidence(rows, target="relationship.trust")
    assert len(kept) == 1


def test_mask_evidence_rejects_unregistered_target():
    """未登记目标 → ValueError（fail-closed：不得为未登记维度开辟证据通道）。"""
    with pytest.raises(ValueError):
        mask_evidence([_row()], target="perspective.favorite_food")


# ─── 证据引用 ───────────────────────────────────────────────────


def test_evidence_ref_formats_chronicle_id():
    assert evidence_ref(_row(row_id=42)) == "chronicle:42"


def test_evidence_ref_requires_id():
    """缺 id → 抛错：无法定位原文的"证据"等于没有证据。"""
    with pytest.raises(ValueError):
        evidence_ref({"kind": "life", "text": "没有 id"})


# ─── 认知模式 ───────────────────────────────────────────────────


def test_cognitive_modes_registered():
    assert COGNITIVE_MODES == ("观察", "转述", "信念", "提议", "条件", "确认")
    assert is_valid_cognitive_mode("条件")
    assert not is_valid_cognitive_mode("猜测")


# ─── 正向信号（事件级，只数行不读 value） ───────────────────────


def test_positive_signal_metrics_are_event_level_only():
    """只有两类事件级指标；断言里不含任何聚合/融合指标（强约束 1 + 禁令 A）。"""
    assert POSITIVE_SIGNAL_METRICS == ("user_initiated_freq", "proactive_replied")
    for banned in ("urge", "urge_factor", "interaction_count", "message_count"):
        assert banned not in POSITIVE_SIGNAL_METRICS


def test_positive_signal_days_dedupes_by_natural_day():
    """同一天多条事件 → 只算 1 个场景日（真实数据去重比最高 18.56）。"""
    store = _FakeMetricsStore(
        {
            "user_initiated_freq": [
                _metric("2026-09-20T09:00:00", "u1"),
                _metric("2026-09-20T09:05:00", "u1"),
                _metric("2026-09-20T21:30:00", "u1"),
            ]
        }
    )
    assert positive_signal_days(store, "u1") == {"2026-09-20"}


def test_positive_signal_days_union_of_two_metrics():
    """两类信号取**并集**（用户主动发起 + 主动消息承接都算正向场景日）。"""
    store = _FakeMetricsStore(
        {
            "user_initiated_freq": [_metric("2026-09-20T09:00:00", "u1")],
            "proactive_replied": [_metric("2026-09-21T09:00:00", "u1")],
        }
    )
    assert positive_signal_days(store, "u1") == {"2026-09-20", "2026-09-21"}
    assert positive_signal_count(store, "u1") == 2


def test_positive_signal_days_isolates_users():
    """跨用户不得串味（按 user_id 精确匹配）。"""
    store = _FakeMetricsStore(
        {
            "user_initiated_freq": [
                _metric("2026-09-20T09:00:00", "u1"),
                _metric("2026-09-21T09:00:00", "u2"),
            ]
        }
    )
    assert positive_signal_days(store, "u1") == {"2026-09-20"}
    assert positive_signal_days(store, "u2") == {"2026-09-21"}


def test_positive_signal_days_ignores_value_semantics():
    """**只数行、不读 value**：value 语义跨版本变过（1.0 计数 vs 延迟分钟）。

    把同一个用户同一天的 value 换成任何东西，结果必须完全相同——
    否则某天有人「顺手 sum 一下」就会静默改变量纲。
    """
    base = [{"ts": "2026-09-20T09:00:00", "scope": "", "user_id": "u1", "value": "1.0"}]
    other = [dict(base[0], value="9999")]
    for value in ("0", "", "1.0", "9999", "not-a-number"):
        rows = [dict(base[0], value=value)]
        store = _FakeMetricsStore({"user_initiated_freq": rows})
        assert positive_signal_days(store, "u1") == {"2026-09-20"}
    assert positive_signal_days(_FakeMetricsStore({"user_initiated_freq": other}), "u1") == {
        "2026-09-20"
    }


def test_positive_signal_days_since_filter():
    store = _FakeMetricsStore(
        {
            "user_initiated_freq": [
                _metric("2026-09-10T09:00:00", "u1"),
                _metric("2026-09-20T09:00:00", "u1"),
            ]
        }
    )
    assert positive_signal_days(store, "u1", since="2026-09-15") == {"2026-09-20"}


def test_positive_signal_days_empty_uid_or_malformed_ts():
    store = _FakeMetricsStore(
        {"user_initiated_freq": [_metric("2026-09-20T09:00:00", "u1"), _metric("坏时间戳", "u1")]}
    )
    assert positive_signal_days(store, "") == set()
    assert positive_signal_days(store, "   ") == set()
    # 坏时间戳被跳过，不污染场景日集合
    assert positive_signal_days(store, "u1") == {"2026-09-20"}


def test_positive_signal_days_missing_metric_file():
    """指标文件不存在（store 返回空列表）→ 空集，不抛。"""
    assert positive_signal_days(_FakeMetricsStore({}), "u1") == set()


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
