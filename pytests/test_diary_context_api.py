"""``narrative_diary_context`` API 的「当日生活素材」测试（2026-10-01 接线）。

背景（用户 2026-10-01 反馈）：

    「今天推送的日记里，大部分都是 bot 昨天在私聊和群聊里的话题，
      自己的生活片段没有进去。」

取证结论**不是**隔离问题（生活片段取材源只读 events 表；diary 产物有
ADR-0004 双向短路），真因是 narrative 早已挂在返回值上的素材字段 diary 侧
一个都没读。修复 = 本 API 按**被写日记的那天**精取当日 ``life_highlight`` /
``life`` 两类编年史，由 diary 的 ``build_narrative_status`` 拼成独立一段。

本文件钉住四条口径：

1. **日期口径**：按入参 ``date`` 精取，而不是"最近 N 条"——日记在次日 04:00
   生成、写的是昨天，此时"最近 N 条"恰好落在今天凌晨；
2. **kind 分流**：高光（``life_highlight``）全部进、按时间升序；普通片段
   （``life``）取最近 4 条、每条截 120 字；``daily`` / ``promotion`` 不进；
3. **ADR-0004 不被本次裁决豁免**：用户虽裁定「涉私与否不区分，全部进日记」，
   但「diary 产物完全隔离」是立项铁律 → kind=diary / source_uid=diary 一律
   短路（含伪装成 life 的那条）；
4. **向后兼容**：``latest_life_fragment`` / ``recent_chronicle`` 两个旧字段
   继续给（旧版 diary 不会因为少了字段炸）。

用**真实** ``NarrativeStore``（sqlite）而非假 store：这里测的正是 SQL 排序、
ts 前缀匹配与读写真口径，用假 store 等于把最容易错的一层跳过。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_diary_context_api.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_diary_context_api.py
"""

from __future__ import annotations

import asyncio
import datetime
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import _synth_loader

_synth_loader.load("services")  # plugin.py 依赖 services/__init__.py 的再导出
_PLUGIN = _synth_loader.load("plugin")
_STORE_MOD = _synth_loader.load("services.store")

MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin
NarrativeStore = _STORE_MOD.NarrativeStore

_DAY = "2026-09-30"
_TODAY = "2026-10-01"
#: 冻结的"插件本地此刻"：10-01 04:00 = 日记刚写完昨天那篇的典型时刻
_FIXED_NOW = datetime.datetime(2026, 10, 1, 4, 0, 0)


class _NullLogger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _AsyncConfig:
    def __init__(self, values: dict):
        self._values = values

    async def get(self, key: str, default=None):
        return self._values.get(key, default)


def _fresh_store() -> NarrativeStore:
    """真实 sqlite store（临时目录；用完不删，与 pytest tmp_path 同款取舍）。"""
    return NarrativeStore(data_dir=Path(tempfile.mkdtemp(prefix="narrative-api-test-")))


def _make_plugin(store: NarrativeStore, *, enabled: bool = True) -> SimpleNamespace:
    """构造可调用 API 的最小 plugin-like 对象（绕开依赖宿主的 __init__）。"""
    state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.55},
            "routine": {"phase": "上午"},
            "focus": {"pending_events": [{"ts": f"{_DAY}T10:00:00", "text": "由头原文"}]},
        }
    }
    return SimpleNamespace(
        _engine=SimpleNamespace(load_self_state=lambda: state),
        _store=store,
        _mode_user_ids=lambda: ["10001"],
        _local_now=lambda: _FIXED_NOW,
        config=SimpleNamespace(
            plugin=SimpleNamespace(enabled=enabled),
            narrative=SimpleNamespace(
                enabled=enabled,
                mode_user_ids=["10001"],
                mode_stream_ids=[],
                timezone_offset_hours=8,
            ),
            identity=SimpleNamespace(world="沿海城市"),
        ),
        ctx=SimpleNamespace(
            config=_AsyncConfig({"personality.personality": "银发狐妖"}),
            logger=_NullLogger(),
        ),
    )


def _call(plugin: SimpleNamespace, **kwargs) -> dict:
    """以未绑定方法调用 API handler（plugin-like 对象冒充 self）。"""
    return asyncio.run(
        MaiNarrativePlugin.handle_narrative_diary_context_api(plugin, **kwargs)
    )


def test_returns_day_highlights_and_recent_fragments():
    """同一天的高光全给（升序）；普通片段取最近 4 条、每条截 120 字。"""
    store = _fresh_store()
    for index in range(6):
        store.append_chronicle("self", "life", f"第{index}段日常", ts=f"{_DAY}T0{index}:30:00")
    store.append_chronicle("self", "life_highlight", "傍晚看到一只三条腿的猫", ts=f"{_DAY}T18:00:00")
    store.append_chronicle("self", "life_highlight", "长时段重复文本" * 60, ts=f"{_DAY}T20:00:00")
    # 干扰项：别的天 + 别的 kind
    store.append_chronicle("self", "life", "别天的片段", ts=f"{_TODAY}T09:00:00")
    store.append_chronicle("self", "daily", "今日小结", ts=f"{_DAY}T23:30:00")
    store.append_chronicle("self", "promotion", "看法更新", ts=f"{_DAY}T22:00:00")

    data = _call(_make_plugin(store), date=_DAY)["self_state"]

    assert data["today_highlights"] == [
        "傍晚看到一只三条腿的猫",
        "长时段重复文本" * 60,
    ], "高光应全给且按时间升序（长文本只受 1024 兜底，不按 120 截）"
    assert data["today_life_fragments"] == [
        "第2段日常",
        "第3段日常",
        "第4段日常",
        "第5段日常",
    ], "普通片段应取**当天最近 4 条**且按时间升序"


def test_ordinary_fragment_truncated_at_120_chars():
    """单条超长普通片段截到 120 字（高光不受此限）。"""
    store = _fresh_store()
    store.append_chronicle("self", "life", "甲乙丙丁" * 100, ts=f"{_DAY}T11:00:00")
    data = _call(_make_plugin(store), date=_DAY)["self_state"]
    assert len(data["today_life_fragments"][0]) == 120


def test_explicit_date_wins_over_local_today():
    """04:00 补写昨天：显式 date 必须压过"本地今天"（本次设计的核心裁决）。"""
    store = _fresh_store()
    store.append_chronicle("self", "life", "昨天的片段", ts=f"{_DAY}T15:00:00")
    store.append_chronicle("self", "life", "今天凌晨的片段", ts=f"{_TODAY}T02:00:00")

    data = _call(_make_plugin(store), date=_DAY)["self_state"]
    assert data["today_life_fragments"] == ["昨天的片段"], "传了 date 就只给那天"

    # 不传 date → 退化为插件时区的今天（_local_now 冻结在 10-01 04:00）
    fallback = _call(_make_plugin(store))["self_state"]
    assert fallback["today_life_fragments"] == ["今天凌晨的片段"]


def test_diary_product_still_dropped_under_2026_10_01_ruling():
    """用户裁定「涉私全部进」，但 diary 产物隔离是铁律 → 仍要短路。"""
    store = _fresh_store()
    # 正规 diary 产物（写入侧自动打 source_uid=diary）
    store.append_chronicle("self", "diary", "昨天的日记正文", ts=f"{_DAY}T04:00:00")
    # 伪装：kind=life 却残留 diary 隔离标记
    store.append_chronicle(
        "self", "life", "混进来的 diary 残片", ts=f"{_DAY}T09:00:00", source_uid="diary"
    )
    # 涉私标记：按 2026-10-01 裁定**要进**（日记是作者本人的私密产物）
    store.append_chronicle(
        "self", "life", "和用户聊过的一段原话", ts=f"{_DAY}T10:00:00", source_uid="10001"
    )

    data = _call(_make_plugin(store), date=_DAY)["self_state"]
    joined = " | ".join(data["today_life_fragments"])
    assert "昨天的日记正文" not in joined
    assert "混进来的 diary 残片" not in joined
    assert data["today_life_fragments"] == ["和用户聊过的一段原话"], (
        "涉私标记不区分（用户裁定），但 diary 标记必须短路（立项铁律）"
    )


def test_empty_day_returns_empty_lists():
    """那天没有片段 → 空列表（不是 None / 不是缺键），diary 侧据此跳过整段。"""
    store = _fresh_store()
    store.append_chronicle("self", "life", "只有今天有", ts=f"{_TODAY}T09:00:00")
    data = _call(_make_plugin(store), date=_DAY)["self_state"]
    assert data["today_highlights"] == []
    assert data["today_life_fragments"] == []


def test_legacy_fields_preserved():
    """旧字段继续给：老版 diary 不会因为缺字段炸，也不会因为多字段炸。"""
    store = _fresh_store()
    store.append_chronicle("self", "life", "一段日常", ts=f"{_DAY}T11:00:00")
    data = _call(_make_plugin(store), date=_DAY)["self_state"]
    assert data["latest_life_fragment"] == "由头原文"
    assert data["recent_chronicle"] == ["一段日常"]
    assert data["mood_label"] == "平静"
    assert data["routine_phase"] == "上午"


def test_not_initialized_returns_unavailable():
    """引擎/存储未初始化 → ok=False + available=False，不抛异常。"""
    broken = _make_plugin(_fresh_store())
    broken._engine = None
    broken._store = None
    assert _call(broken, date=_DAY) == {
        "ok": False,
        "available": False,
        "error": "not_initialized",
    }


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
