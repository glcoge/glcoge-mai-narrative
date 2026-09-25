"""指标 4 双轨埋点 + 晋升计数器通道 + status 脱敏测试（批 1 · C5 / B9+B10+B12）。

- **指标 4 双轨**（R24 A 轨 / R7 B 轨）：A=注入侧状态组合熵，B=产出侧字符 n-gram
  去重率（n=3~5）。读法：**A 变 B 不变 ＝ 只长数字不长故事**；铺群闸门挂 B 轨（R8/P11）。
- **晋升计数器**（R17）：proposals / promotions / refutations / rollbacks 四个 csv，
  批 1 只建通道（批 4 才写入），未登记 kind 直接抛错——计数器写错比不写更糟。
- **N2 脱敏**（D10/Q5）：status 文本曾把 mode_user_ids 原文外发给非管理员会话
  （2026-09-22 真实事故），改为只显示计数。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_metrics_diversity.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_metrics_diversity.py
"""

from __future__ import annotations

import asyncio
import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

import _synth_loader

_synth_loader.load("services.state.engine")
_SNAPSHOT = _synth_loader.load("services.state.snapshot")
_SYNTH_SERVICES = _synth_loader.load("services")  # plugin.py 依赖 services/__init__.py
_PLUGIN_MOD = _synth_loader.load("plugin")

Telemetry = _SNAPSHOT.Telemetry
ngram_diversity = _SNAPSHOT.ngram_diversity
shannon_entropy = _SNAPSHOT.shannon_entropy
state_combo_key = _SNAPSHOT.state_combo_key
MaiNarrativePlugin = _PLUGIN_MOD.MaiNarrativePlugin

_NOW = datetime.datetime(2026, 9, 22, 11, 37, 0)
_QQ = "927386371"


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


class _FakeStore:
    def __init__(self):
        self.metrics: list = []
        self.kv_int: dict = {}

    def append_metric(self, name, value, user_id="", scope="", ts=None):
        self.metrics.append((name, value, scope))

    def get_kv_int(self, key, default=0):
        return int(self.kv_int.get(key, 0))

    def recent_chronicle(self, scope, limit=5):
        return []


def _make_telemetry():
    plugin = SimpleNamespace(
        config=SimpleNamespace(plugin=SimpleNamespace(enabled=True),
                               telemetry=SimpleNamespace(enabled=True)),
        ctx=SimpleNamespace(logger=_Logger()),
    )
    store = _FakeStore()
    plugin._store = store
    return Telemetry(plugin), store


def _state(mood="平静", phase="午后", energy=0.6):
    return {"state": {"mood": {"label": mood, "energy": energy}, "routine": {"phase": phase}}}


# ===== 指标 4 · B 轨：产出多样性（R7/P5） =====


def test_ngram_diversity_identical_texts_are_low():
    """同一句话重复产出去重率必然低（这正是"只长数字不长故事"的信号）。"""
    repeated = ["去海边走了走风很大"] * 4
    varied = ["去海边走了走风很大", "把坏掉的台灯修好了", "煮了一锅糊掉的粥", "给猫剪了指甲"]
    assert ngram_diversity(repeated) < ngram_diversity(varied)


def test_ngram_diversity_ignores_punctuation():
    """标点抖动不算多样性：只差标点的两句，去重率与"完全重复"一致。"""
    punctuated = ngram_diversity(["今天，下雨了！", "今天下雨了"])
    identical = ngram_diversity(["今天下雨了", "今天下雨了"])
    assert punctuated == identical, "去标点后两者应等价（标点不算新说法）"


def test_ngram_diversity_empty_is_zero():
    assert ngram_diversity([]) == 0.0
    assert ngram_diversity([""]) == 0.0


# ===== 指标 4 · A 轨：状态组合熵（R24） =====


def test_shannon_entropy_basics():
    assert shannon_entropy(["a", "a", "a"]) == 0.0, "取值全同 → 熵为 0"
    assert shannon_entropy(["a", "b"]) == 1.0, "两种取值均匀分布 → 1 bit"
    assert shannon_entropy(["a"]) == 0.0, "样本不足 2 条 → 0"


def test_state_combo_key_covers_mood_phase_energy_band():
    key = state_combo_key(_state(mood="轻快", phase="清晨", energy=0.75))
    assert "轻快" in key and "清晨" in key
    assert key != state_combo_key(_state(mood="平静", phase="清晨", energy=0.75))


# ===== 双轨同时采样 =====


def test_note_fragment_records_both_tracks():
    """一次产出必须同时落 A、B 两轨（否则两侧不同尺度，无法对照）。"""
    telemetry, store = _make_telemetry()
    telemetry.note_fragment(_state(), "去海边走了走，风很大")
    names = [name for name, _v, _s in store.metrics]
    assert "state_diversity" in names, "缺 A 轨（状态组合熵）"
    assert "output_diversity" in names, "缺 B 轨（产出 n-gram 多样性）"


def test_note_fragment_window_is_bounded():
    """滚动窗口有界：不能让内存随运行时间无限涨。"""
    telemetry, _store = _make_telemetry()
    for index in range(50):
        telemetry.note_fragment(_state(energy=0.1 + index / 100), f"第{index}段生活")
    assert len(telemetry._diversity_outputs) == telemetry._diversity_outputs.__len__()
    assert len(telemetry._diversity_outputs) <= _SNAPSHOT._DIVERSITY_WINDOW


# ===== 晋升计数器通道（R17） =====


def test_counter_kinds_all_writable():
    for kind in ("proposals", "promotions", "refutations", "rollbacks"):
        telemetry, store = _make_telemetry()
        telemetry.record_counter(kind)
        assert store.metrics == [(kind, 1, "promotion")]


def test_counter_unknown_kind_rejected():
    """未登记 kind 直接抛错——计数器写错比不写更糟。"""
    telemetry, _store = _make_telemetry()
    try:
        telemetry.record_counter("whatever")
    except ValueError:
        return
    raise AssertionError("未知计数器类型应抛 ValueError")


# ===== N2 脱敏（D10/Q5） =====


class _FakeSend:
    def __init__(self):
        self.texts: list = []

    async def text(self, content, stream_id):
        self.texts.append(content)


def _make_plugin_for_status():
    plugin = MaiNarrativePlugin.__new__(MaiNarrativePlugin)
    store = _FakeStore()
    engine = SimpleNamespace(
        load_self_state=lambda: {
            "state": {
                "mood": {"label": "平静", "energy": 0.6},
                "routine": {"phase": "午后"},
                "focus": {"hot_thread": "", "pending_events": []},
            }
        },
        load_branch_state=lambda uid: {
            "identity": {"stage": "熟人"},
            "state": {"familiarity": 30.0, "trust": 40.0},
        },
    )
    send = _FakeSend()
    # SDK 的 plugin.config / plugin.ctx 是只读 property，测试里设私有实例属性
    plugin._plugin_config_instance = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=_synth_loader.sleep_config(
            enabled=True, mode_user_ids=[_QQ, "3401922770"], sleep_time="", wake_time=""
        ),
        proactive=SimpleNamespace(enabled=True),
        llm=SimpleNamespace(creation_model="my-model", temperature=0.9),
    )
    plugin._engine = engine
    plugin._store = store
    plugin._streams = SimpleNamespace(known_count=lambda: 2)
    plugin._telemetry = None
    plugin._ctx = SimpleNamespace(
        send=send,
        logger=_Logger(),
        paths=SimpleNamespace(data_dir=Path("D:/data")),
        llm=SimpleNamespace(get_available_models=lambda: _raise()),
    )
    plugin._local_now = lambda: _NOW
    plugin._mode_user_ids = lambda: [_QQ, "3401922770"]
    return plugin, send


def _raise():
    raise RuntimeError("no llm")


def test_status_does_not_leak_user_ids():
    """status 文本不得出现任何模式用户号（N2：曾原文外发给第三方会话）。"""
    plugin, send = _make_plugin_for_status()
    asyncio.run(plugin._cmd_status("stream-1", _QQ))
    output = "\n".join(send.texts)
    assert _QQ not in output, f"status 泄露了用户号: {output}"
    assert "3401922770" not in output
    assert "2 人" in output, "应改为显示计数"


def test_status_still_shows_branch_lines_with_index():
    """脱敏后支线行改用序号代号，信息不丢（阶段/熟悉/信任仍在）。"""
    plugin, send = _make_plugin_for_status()
    asyncio.run(plugin._cmd_status("stream-1", _QQ))
    output = "\n".join(send.texts)
    assert "支线[1]" in output and "支线[2]" in output
    assert "熟人" in output


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
