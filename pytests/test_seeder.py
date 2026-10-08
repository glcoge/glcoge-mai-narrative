"""世界事件源（播种器）测试（v0.3.0 批 2 / R40 / 总览 §6.2 输入②）。

覆盖四块：
- **四闸节奏**：enabled（默认关）/ interval / probability / daily_max 全走配置，
  rng 用 life 高光签同款手法（monkeypatch ``seeder._rng``）钉住；
- **生成**：prompt 携带禁令段（红线①第一层）+ 世界基调 + NPC 名册；JSON 解析失败
  降级为默认元数据不丢弃产出；
- **🔴 参与者禁入第二层**：落库前词面拦截（uid/gid/手工网名），命中**丢弃整条**
  + WARNING + 计数；
- **落账**：事件实体 kind=seed（含 importance/urgency 元数据）+ 编年史 life_seed
  双写；空名册降级生成（NPC 题材缺席，不空转）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_seeder.py -q
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import _synth_loader
from pytests._synth_loader import KvStoreMixin, load  # noqa: E402
from pytests._synth_loader import FakeLogger, FakeStore, make_logger  # noqa: E402

_SEEDER = load("services.creation.seeder")
_LOADER = load("services.lorebook.loader")

maybe_seed_world_event = _SEEDER.maybe_seed_world_event
build_seed_prompt = _SEEDER.build_seed_prompt

_NOW = datetime.datetime(2026, 10, 7, 15, 0, 0)
_UID = "10001"

_GOOD_JSON = (
    '{"text": "巷口面馆的老板娘今早进了新米，蒸笼比平时多摆了两层。", '
    '"importance": "mid", "urgency": "short"}'
)
_GOOD_TEXT = "巷口面馆的老板娘今早进了新米，蒸笼比平时多摆了两层。"

_BOOK_WITH_CAST = (
    '[[entries]]\n'
    'name = "临海市"\n'
    'content = "一座常年起海雾的沿海城市。"\n'
    'kind = "world"\n'
    'constant = true\n'
    '\n'
    '[[entries]]\n'
    'name = "面馆老板娘"\n'
    'content = "巷口面馆的老板娘，记性好得出奇。"\n'
    'kind = "cast"\n'
)

_BOOK_NO_CAST = (
    '[[entries]]\n'
    'name = "临海市"\n'
    'content = "一座常年起海雾的沿海城市。"\n'
    'kind = "world"\n'
    'constant = true\n'
)






class _FakeCreator:
    def __init__(self, text):
        self.text = text
        self.prompts: list = []

    async def generate(self, prompt):
        self.prompts.append(prompt)
        return self.text


class _FakeTelemetry:
    def __init__(self):
        self.samples: list = []

    def record(self, name, value=1, user_id="", scope=""):
        self.samples.append({"name": name, "value": value, "scope": scope})


def _make_engine(
    *,
    seeder=None,
    creator_text=_GOOD_JSON,
    lorebook_text=_BOOK_WITH_CAST,
    mode_user_ids=(_UID,),
    known_uids=(),
    known_gids=(),
):
    """播种链路最小 engine（四闸 + 生成 + 拦截 + 落账所需依赖齐备）。"""
    if seeder is None:
        seeder = _synth_loader.seeder_config(enabled=True)
    config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True),
        narrative=SimpleNamespace(
            enabled=True,
            chronicle_enabled=True,
            mode_user_ids=list(mode_user_ids),
            fragment_pending_max=12,
        ),
        llm=SimpleNamespace(show_prompt=False, temperature=0.7),
        identity=SimpleNamespace(world="", values=[], world_rules=[], guard_fragments=[]),
        anchor=SimpleNamespace(guard_keywords=[]),
        seeder=seeder,
    )
    logger = FakeLogger()
    store = FakeStore()
    plugin = SimpleNamespace(config=config, ctx=SimpleNamespace(logger=logger), _store=store)
    plugin._lorebook = _make_loader(lorebook_text)
    if known_uids:
        plugin._streams = SimpleNamespace(known_uids=lambda: list(known_uids))
    if known_gids:
        plugin._group_streams = SimpleNamespace(known_gids=lambda: list(known_gids))
    engine = load("services.state.engine").NarrativeEngine.__new__(
        load("services.state.engine").NarrativeEngine
    )
    engine._plugin = plugin
    engine._store = store
    engine._creator = _FakeCreator(creator_text)
    engine._self_state = {
        "state": {
            "mood": {"label": "平静", "energy": 0.6},
            "routine": {"phase": "午后", "sleep_state": "awake"},
            "focus": {"pending_events": []},
        }
    }
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda state: None
    return engine


def _make_loader(book_text):
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "lorebook.toml"
    path.write_text(book_text, encoding="utf-8")
    return _LOADER.LorebookLoader(path, budget=800, max_entries=100)


def _pending(engine):
    return engine._self_state["state"]["focus"]["pending_events"]


# ===== 四闸节奏 =====


def test_disabled_noop(monkeypatch):
    """enabled=false（默认）→ 完全不动（不调 LLM、不写 kv）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(seeder=_synth_loader.seeder_config(enabled=False))
    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert engine._creator.prompts == []
    assert engine._store.get_kv_str("seed:last_try") == ""


def test_interval_gate_blocks_recent_try(monkeypatch):
    """interval 闸：距上次尝试未满 interval_minutes → 不生成。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        seeder=_synth_loader.seeder_config(enabled=True, interval_minutes=240)
    )
    recent = (_NOW - datetime.timedelta(minutes=60)).isoformat(timespec="seconds")
    engine._store.set_kv_str("seed:last_try", recent)

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert engine._creator.prompts == []


def test_interval_elapsed_advances_last_try(monkeypatch):
    """interval 已过 → 尝试一次并推进 last_try（本 interval 内不重试）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        seeder=_synth_loader.seeder_config(enabled=True, interval_minutes=240)
    )
    old = (_NOW - datetime.timedelta(minutes=300)).isoformat(timespec="seconds")
    engine._store.set_kv_str("seed:last_try", old)

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert engine._store.get_kv_str("seed:last_try") == _NOW.isoformat(timespec="seconds")


def test_probability_gate_fails(monkeypatch):
    """概率闸：rng ≥ probability → 本轮放弃（last_try 已推进）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.99)
    engine = _make_engine(
        seeder=_synth_loader.seeder_config(enabled=True, probability=0.5)
    )
    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert engine._creator.prompts == []
    assert engine._store.get_kv_str("seed:last_try") == _NOW.isoformat(timespec="seconds")


def test_daily_max_blocks(monkeypatch):
    """每日上限：当日已播满 daily_max → 不再生成。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(seeder=_synth_loader.seeder_config(enabled=True, daily_max=2))
    engine._store.set_kv_int("seed:count:2026-10-07", 2)

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert engine._creator.prompts == []


# ===== 生成与落账 =====


def test_success_double_write(monkeypatch):
    """正链路：事件实体（kind=seed+元数据）+ 编年史 life_seed 双写 + 计数。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine()

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))

    pending = _pending(engine)
    assert len(pending) == 1
    entry = pending[0]
    assert entry["kind"] == "seed"
    assert entry["text"] == _GOOD_TEXT
    assert entry["importance"] == "mid"
    assert entry["urgency"] == "short"
    assert entry["highlight"] is False
    assert entry["event_id"].startswith("ev_")
    assert engine._store.chronicle[0]["kind"] == "life_seed"
    assert engine._store.chronicle[0]["text"] == _GOOD_TEXT
    assert engine._store.get_kv_int("seed:count:2026-10-07") == 1


def test_prompt_carries_ban_rule_and_context(monkeypatch):
    """红线①第一层：prompt 必含禁令段；且携带世界基调与 NPC 名册素材。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    monkeypatch.setattr(_SEEDER._rng, "sample", lambda pool, k: pool[:k])
    engine = _make_engine()

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))

    prompt = engine._creator.prompts[0]
    assert "不得涉及任何真实用户" in prompt, "禁令段是模板常量，缺失即红线破防"
    assert "一座常年起海雾的沿海城市" in prompt, "world 条目 = 世界基调"
    assert "面馆老板娘" in prompt, "cast 名册 = NPC 取材面"
    assert "importance" in prompt, "输出格式要求（JSON 元数据）"


def test_participant_uid_blocked(monkeypatch):
    """红线①第二层：产出含模式用户号 → 整条丢弃（不入库、计数、WARNING 带原文）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        creator_text=(
            '{"text": "10001 在巷口开了一家新的杂货店。", "importance": "low", "urgency": "short"}'
        )
    )
    telemetry = _FakeTelemetry()
    engine._plugin._telemetry = telemetry

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))

    assert _pending(engine) == [], "命中参与者 → 不入库"
    assert engine._store.chronicle == []
    assert engine._store.get_kv_int("seed:count:2026-10-07") == 0, "被拦的不占每日上限"
    assert any("参与者拦截" in w for w in engine._plugin.ctx.logger.warnings)
    assert any(s["name"] == "seed_blocked" for s in telemetry.samples), "拦截必须计数"


def test_blocked_name_hit(monkeypatch):
    """手工网名表：blocked_names 命中同样丢弃。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        creator_text=(
            '{"text": "小明的杂货店今天开张了。", "importance": "low", "urgency": "short"}'
        ),
        seeder=_synth_loader.seeder_config(enabled=True, blocked_names=["小明"]),
    )

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert _pending(engine) == []


def test_gid_blocked(monkeypatch):
    """群号拦截：known_gids 命中丢弃。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        creator_text=(
            '{"text": "987654 这个小区今晚停电。", "importance": "low", "urgency": "short"}'
        ),
        known_gids=("987654",),
    )

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert _pending(engine) == []


def test_streams_uid_blocked(monkeypatch):
    """StreamRegistry 全量已知 uid 拦截（覆盖非模式用户）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        creator_text=(
            '{"text": "55667788 最近总在河边跑步。", "importance": "low", "urgency": "short"}'
        ),
        mode_user_ids=(),
        known_uids=("55667788",),
    )

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert _pending(engine) == []


def test_parse_failure_downgrades(monkeypatch):
    """JSON 解析失败 → 降级为纯文本 + 默认元数据（不丢弃产出，WARNING 计数）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(creator_text="河边有人在放风筝，风把线吹得很斜。")

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))

    entry = _pending(engine)[0]
    assert entry["kind"] == "seed"
    assert entry["text"] == "河边有人在放风筝，风把线吹得很斜。"
    assert entry["importance"] == "low"
    assert entry["urgency"] == "short"
    assert engine._plugin.ctx.logger.warnings, "降级必须打 WARNING"


def test_empty_cast_still_generates(monkeypatch):
    """空名册降级：NPC 题材缺席但世界/日常事件照常（不空转、不报错）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(lorebook_text=_BOOK_NO_CAST)

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))

    prompt = engine._creator.prompts[0]
    assert "面馆老板娘" not in prompt
    assert "一座常年起海雾的沿海城市" in prompt
    assert len(_pending(engine)) == 1


def test_generation_empty_text_noop(monkeypatch):
    """LLM 返回空 → 直接返回（不计入每日上限，下个 interval 再试）。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(creator_text="")

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert _pending(engine) == []
    assert engine._store.get_kv_int("seed:count:2026-10-07") == 0


def test_blocked_does_not_consume_quota(monkeypatch):
    """拦截丢弃不占每日上限：拦 1 条后同日仍可再播满 daily_max。"""
    monkeypatch.setattr(_SEEDER._rng, "random", lambda: 0.0)
    engine = _make_engine(
        creator_text=(
            '{"text": "10001 的杂货店开张了。", "importance": "low", "urgency": "short"}'
        ),
        seeder=_synth_loader.seeder_config(enabled=True, daily_max=1),
    )
    engine._plugin._telemetry = _FakeTelemetry()

    import asyncio

    asyncio.run(maybe_seed_world_event(engine.deps, _NOW))
    assert _pending(engine) == []
    assert engine._store.get_kv_int("seed:count:2026-10-07") == 0, "拦截不消耗配额"


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
