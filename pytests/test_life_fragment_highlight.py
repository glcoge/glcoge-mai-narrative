"""生活片段「高光签」测试（2026-09-30，方案 §5 / Q2/Q3/Q5/Q8/Q14 / P19）。

替代原 ``test_life_fragment_tier.py``（三信号定档随 R22 一并废止）：

| 旧设计 | 新设计 |
|---|---|
| 三档判定（minor/normal/major），三信号规则 | 二元抽签（高光/非高光），``_draw_highlight`` |
| 长度区间表（40~90 / 80~180 / 200~400） | ❌ 整表删除 → 零数字「因果句」 |
| 素材展示上限按档（80/120/300） | 统一 120（§15-1） |

覆盖：
- **抽签**：开关关闭永不抽签 / 起床豁免 / 日上限封顶 / 概率边界（monkeypatch ``_rng``）
- **落库**：抽中 → pending ``highlight=true`` + chronicle ``kind=life_highlight`` + 计数 +1
          未中 → ``highlight=false`` + ``kind=life``
- **prompt**：基础句零数字 / 高光追加四段式 / 素材上限统一
- **渲染侧**（自 tier 文件原样迁移）：400 字不截 / 1024 硬顶 / 由头全片段
"""

from __future__ import annotations

import asyncio
import datetime
import re
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import KvStoreMixin, load  # noqa: E402

_LIFE = load("services.creation.life")
_NarrativeEngine = load("services.state.engine").NarrativeEngine
life = _LIFE
sourcing = load("services.proactive.sourcing")
_RENDER = load("services.render.planner_block")

build_context_block = _RENDER.build_context_block
build_life_fragment_prompt = _LIFE.build_life_fragment_prompt

_NOW = datetime.datetime(2026, 9, 21, 12, 0, 0)
_TODAY = "2026-09-21"
_UID = "10001"


class _Logger:
    def __init__(self):
        self.infos: list = []

    def info(self, *a, **k):
        self.infos.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeStore(KvStoreMixin):
    """只含生活片段链路所需接口的假 store。"""

    def __init__(self):
        self.chronicle: list = []
        self.kv_int: dict = {}
        self.kv_str: dict = {}

    def get_kv_int(self, key):
        return self.kv_int.get(key, 0)

    def set_kv_int(self, key, value):
        self.kv_int[key] = value

    def get_kv_str(self, key):
        return self.kv_str.get(key, "")

    def set_kv_str(self, key, value):
        self.kv_str[key] = value

    def list_events(self, scope, limit=20):
        return []

    def append_chronicle(self, scope, kind, text, ts):
        self.chronicle.append({"scope": scope, "kind": kind, "text": text, "ts": ts})


class _FakeCreator:
    async def generate(self, prompt):
        return "今天在厨房煮了粥。"


def _make_engine(
    *, detail_enabled=True, probability=0.12, interval=30, daily_max=16, pending_max=12
):
    """构造只含生活片段链路依赖的 engine（走 engine 的薄委托方法）。"""
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            chronicle_enabled=True,
            mode_user_ids=[_UID],
            life_fragment_daily_max=daily_max,
            life_fragment_interval_minutes=interval,
            life_fragment_detail_enabled=detail_enabled,
            highlight_probability=probability,
            fragment_pending_max=pending_max,
            sleep_time="",
            wake_time="",
            wake_fragment_enabled=False,
            sleep_pre_sleep_hint_minutes=25,
        ),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
        identity=SimpleNamespace(world="", values=[], world_rules=[], guard_fragments=[]),
        anchor=SimpleNamespace(guard_keywords=[]),
    )
    logger = _Logger()
    engine = _NarrativeEngine.__new__(_NarrativeEngine)
    engine._plugin = SimpleNamespace(
        config=config, ctx=SimpleNamespace(logger=logger), _telemetry=None
    )
    engine._store = _FakeStore()
    engine._creator = _FakeCreator()
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6},
            "routine": {"phase": "白天", "sleep_state": "awake"},
            "focus": {"pending_events": []},
        }
    }
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    engine.load_branch_state = lambda uid: {"relationship": {"milestones": []}}
    return engine


def _pending(engine):
    return engine._self_state["state"]["focus"]["pending_events"]


def _block_rng(monkeypatch):
    """把 RNG 换成「一调就炸」的哨兵——用于证明某条支路压根没抽签。"""

    def _boom():
        raise AssertionError("本条支路不应调用 RNG（应在抽签前短路）")

    monkeypatch.setattr(_LIFE._rng, "random", _boom)


def _fixed_rng(monkeypatch, value):
    monkeypatch.setattr(_LIFE._rng, "random", lambda: value)


# ===== 抽签：三条豁免 / 上限 / 概率边界 =====


def test_switch_off_never_draws(monkeypatch):
    """总开关关闭 → 直接 False，且**不碰 RNG**（唯一降级开关，Q8）。"""
    engine = _make_engine(detail_enabled=False)
    _block_rng(monkeypatch)
    assert _LIFE._draw_highlight(engine.deps, _TODAY, wake=False) is False


def test_wake_fragment_exempt_from_draw(monkeypatch):
    """起床片段豁免抽签：它是状态转换的必然产物，不碰 RNG（Q5）。"""
    engine = _make_engine(detail_enabled=True)
    _block_rng(monkeypatch)
    assert _LIFE._draw_highlight(engine.deps, _TODAY, wake=True) is False


def test_daily_max_caps_draw(monkeypatch):
    """当日已抽中数达上限 → False，不碰 RNG（防「一天全是高光」，P19）。"""
    engine = _make_engine()
    engine._store.set_kv_int(f"life_fragment:highlight:count:{_TODAY}", _LIFE._HIGHLIGHT_DAILY_MAX)
    _block_rng(monkeypatch)
    assert _LIFE._draw_highlight(engine.deps, _TODAY, wake=False) is False


def test_probability_boundary(monkeypatch):
    """概率边界：RNG < p 抽中，≥ p 落空（p=0.12）。"""
    engine = _make_engine(probability=0.12)
    _fixed_rng(monkeypatch, 0.11)
    assert _LIFE._draw_highlight(engine.deps, _TODAY, wake=False) is True
    _fixed_rng(monkeypatch, 0.13)
    assert _LIFE._draw_highlight(engine.deps, _TODAY, wake=False) is False


# ===== 落库三件套（Q14） =====


def test_highlight_writes_three_pieces(monkeypatch):
    """抽中 → pending.highlight=true + chronicle kind=life_highlight + 当日计数 +1。"""
    engine = _make_engine()
    _fixed_rng(monkeypatch, 0.0)  # 必中
    asyncio.run(life.maybe_generate_life_fragment(engine.deps,_NOW))

    item = _pending(engine)[-1]
    assert item["highlight"] is True, "抽中片段落库应带 highlight=true"
    kinds = [row["kind"] for row in engine._store.chronicle]
    assert kinds == ["life_highlight"], f"抽中应落 kind=life_highlight，实际 {kinds}"
    assert engine._store.get_kv_int(f"life_fragment:highlight:count:{_TODAY}") == 1


def test_plain_fragment_writes_life_kind(monkeypatch):
    """未抽中 → highlight=false + kind=life + 高光计数不推进。"""
    engine = _make_engine()
    _fixed_rng(monkeypatch, 0.99)  # 必不中
    asyncio.run(life.maybe_generate_life_fragment(engine.deps,_NOW))

    item = _pending(engine)[-1]
    assert item["highlight"] is False, "未抽中应带 highlight=false"
    kinds = [row["kind"] for row in engine._store.chronicle]
    assert kinds == ["life"], f"未抽中应落 kind=life，实际 {kinds}"
    assert engine._store.get_kv_int(f"life_fragment:highlight:count:{_TODAY}") == 0


def test_highlight_count_not_advanced_when_switch_off(monkeypatch):
    """开关关闭 → 永不抽签，计数键始终为 0（防「关了还在计数」）。"""
    engine = _make_engine(detail_enabled=False)
    _block_rng(monkeypatch)
    asyncio.run(life.maybe_generate_life_fragment(engine.deps,_NOW))

    assert _pending(engine)[-1]["highlight"] is False
    assert engine._store.get_kv_int(f"life_fragment:highlight:count:{_TODAY}") == 0


# ===== prompt 改造（Q3） =====


def _prompt(engine, *, highlight, materials=()):
    return build_life_fragment_prompt(
        engine.deps, _NOW, engine._self_state, list(materials), persona="一只小麒麟",
        highlight=highlight,
    )


def test_prompt_causal_no_numbers():
    """基础句**零数字**：不得再出现任何「NN~NN 字」区间（Q3，HDSI 教训）。"""
    engine = _make_engine()
    text = _prompt(engine, highlight=False)
    assert not re.search(r"\d+\s*[~\-－]\s*\d+\s*字", text), "prompt 不应再含字数区间"
    assert "篇幅跟着真实发生的事走" in text, "应改为「篇幅是结果」的因果句"
    assert "请以第一人称" in text


def test_prompt_highlight_asks_four_parts():
    """高光追加句要求四段式（起因/感受/画面/认知），非高光不得出现。"""
    engine = _make_engine()
    hot = _prompt(engine, highlight=True)
    for keyword in ("起因", "感受", "画面", "认知"):
        assert keyword in hot, f"高光 prompt 缺「{keyword}」要求"
    assert "不太寻常" in hot
    plain = _prompt(engine, highlight=False)
    assert "不太寻常" not in plain, "非高光片段不得追加高光要求"


def test_prompt_material_cap_unified():
    """素材展示上限统一 120（§15-1）：不再随档位变。"""
    engine = _make_engine()
    long_material = "甲" * 400
    for highlight in (False, True):
        text = _prompt(engine, highlight=highlight, materials=[long_material])
        assert "甲" * 120 in text, "素材应保留到 120 字符"
        assert "甲" * 121 not in text, "超过 120 字符的部分必须截掉"


# ===== 渲染侧：细节要活到主 prompt（自 tier 文件迁移） =====


def _render_with_fragment(fragment: str) -> str:
    config = SimpleNamespace(
        identity=SimpleNamespace(world="海边小城", values=[], world_rules=[], immutable_traits=[]),
        narrative=SimpleNamespace(
            sleep_time="",
            wake_time="",
            sleep_pre_sleep_hint_minutes=25,
            woken_awake_minutes=30,
        ),
    )
    plugin = SimpleNamespace(config=config)
    state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.45, "last_shift_ts": ""},
            "routine": {"phase": "上午", "sleep_state": "awake"},
            "focus": {"pending_events": [{"ts": "2026-09-21T11:00:00", "text": fragment}]},
            "last_interaction_ts": "",
            "last_talk_date": "",
        }
    }
    return build_context_block(plugin, state, None, _NOW, [])


def test_render_keeps_full_fragment():
    """高光片段可能很长：应整体进入主 prompt，不再被旧上限截断。"""
    fragment = "乙" * 400
    text = _render_with_fragment(fragment)
    assert ("乙" * 400) in text, "400 字片段不应被截断"
    assert ("乙" * 401) not in text, "原文只有 400 字"


def test_render_hard_cap_prevents_runaway():
    """上限 1024 是防失控天花板（不是调节旋钮）：超长文本仍要被截住。"""
    fragment = "乙" * 5000
    text = _render_with_fragment(fragment)
    assert ("乙" * 1024) in text, "应保留到 1024 字"
    assert ("乙" * 1025) not in text, "超过 1024 字的部分必须截掉"


def test_bysource_keeps_full_fragment():
    """主动开口的由头不再只取前 60 字（旧值会把长片段截掉）。"""
    engine = _make_engine()
    fragment = "甲" * 300
    _pending(engine).append({"ts": "2026-09-21T11:00:00", "text": fragment, "highlight": False})
    bysource = sourcing.build_bysource(engine.deps,_UID, _NOW)
    assert "甲" * 300 in bysource, "由头应带完整片段（旧上限 60 字会截断）"
    assert "甲" * 301 not in bysource


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
