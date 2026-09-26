"""冷启动 seed（v0.2.0 批 4 · C5 / R15）：从锚定层派生 perspective 初值。

seed 的存在理由（方案 §3.3）：不设初值则「现值」不存在，「她的看法何时、因何证据
变过」无从叙述——晋升 diff 没有起点。因此本文件锁三件事：

- **只派生、不补充人设**：prompt 明说「初始基线、可被推翻」，输出只写
  ``perspective.world_view`` / ``perspective.life_goals``，不碰锚定层。
- **失败不阻断**：``failed`` / ``empty`` 留空返回，下次启动会再试（未播种状态不被跳过）。
- **幂等**：已播种（origin 非空或两维有内容）直接 ``already``，绝不重复调 LLM。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_slow_seed.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_slow_seed.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import tempfile
import types
from pathlib import Path

import _synth_loader

_PROPOSAL = _synth_loader.load("services.learning.proposal")
_ENGINE = _synth_loader.load("services.state.engine")
_STORE = _synth_loader.load("services.store")

build_seed_prompt = _PROPOSAL.build_seed_prompt
parse_seed = _PROPOSAL.parse_seed
seed_perspective = _PROPOSAL.seed_perspective
default_self_state = _ENGINE.default_self_state
NarrativeStore = _STORE.NarrativeStore

NOW = datetime.datetime(2026, 9, 26, 12, 0, 0)


class _FakeClient:
    """假 LLM 客户端（记录 prompt，可注入失败 / 非 JSON）。"""

    def __init__(self, response: str = "", *, fail: bool = False):
        self._response = response
        self._fail = fail
        self.calls: list = []

    async def generate(self, prompt: str, *, temperature=None) -> str:
        self.calls.append({"prompt": prompt, "temperature": temperature})
        return "" if self._fail else self._response


class _FakeEngine:
    """最小引擎假件：self state 读写（seed 只碰 self state）。"""

    def __init__(self, state=None):
        self.self_state = state if state is not None else default_self_state()
        self.save_count = 0

    def load_self_state(self):
        return self.self_state

    def save_self_state(self, state):
        self.self_state = state
        self.save_count += 1


def _seed_json(world_view="她相信世界比想象的大", goals=None) -> str:
    return json.dumps(
        {"world_view": world_view, "life_goals": ["学会游泳"] if goals is None else goals},
        ensure_ascii=False,
    )


def _make(
    tmp: str,
    *,
    response="",
    fail=False,
    state=None,
    world="普通现代都市",
    values=("诚实", "不伤人"),
    store=None,
    engine=None,
    **config_overrides,
):
    """构造 (plugin, store, engine, fake_client)。``CreatorClient`` 由外层 monkeypatch。"""
    store = store if store is not None else NarrativeStore(Path(tmp))
    engine = engine if engine is not None else _FakeEngine(state)
    client = _FakeClient(response, fail=fail)
    plugin = types.SimpleNamespace(
        _store=store,
        _engine=engine,
        config=types.SimpleNamespace(
            promotion=_synth_loader.promotion_config(**config_overrides),
            identity=types.SimpleNamespace(world=world, values=list(values)),
        ),
        ctx=types.SimpleNamespace(logger=_synth_loader.null_logger()),
    )
    return plugin, store, engine, client


class _PatchedClient:
    """把模块级 ``CreatorClient`` 临时换成返回固定假件的工厂（seed 内部自行构造）。"""

    def __init__(self, client):
        self._client = client
        self._saved = None

    def __enter__(self):
        self._saved = _PROPOSAL.CreatorClient
        _PROPOSAL.CreatorClient = lambda plugin: self._client  # noqa: E731
        return self._client

    def __exit__(self, *exc):
        _PROPOSAL.CreatorClient = self._saved
        return False


# ─── prompt 组装 ────────────────────────────────────────────────


def test_prompt_carries_world_and_values():
    prompt = build_seed_prompt("塞博朋克沿海城市", ["诚实", "不伤人"])
    assert "塞博朋克沿海城市" in prompt
    assert "诚实" in prompt and "不伤人" in prompt


def test_prompt_marks_empty_anchor():
    prompt = build_seed_prompt("", [])
    assert prompt.count("（未填写）") == 2


def test_prompt_frames_as_baseline_not_law():
    """措辞必须强调「初始基线、可被推翻」——seed 不是不可动摇的人设副本。"""
    prompt = build_seed_prompt("普通现代都市", ["诚实"])
    assert "初始基线" in prompt
    assert "可被推翻" in prompt


# ─── 解析 ───────────────────────────────────────────────────────


def test_parse_seed_plain_object():
    parsed = parse_seed(_seed_json())
    assert parsed == {"world_view": "她相信世界比想象的大", "life_goals": ["学会游泳"]}


def test_parse_seed_handles_markdown_fence():
    raw = "```json\n" + _seed_json() + "\n```"
    assert parse_seed(raw)["world_view"] == "她相信世界比想象的大"


def test_parse_seed_only_world_view():
    parsed = parse_seed(json.dumps({"world_view": "世界很大"}, ensure_ascii=False))
    assert parsed == {"world_view": "世界很大", "life_goals": []}


def test_parse_seed_only_goals():
    parsed = parse_seed(json.dumps({"life_goals": ["学会游泳"]}, ensure_ascii=False))
    assert parsed == {"world_view": "", "life_goals": ["学会游泳"]}


def test_parse_seed_ignores_non_list_goals():
    parsed = parse_seed(json.dumps({"world_view": "x", "life_goals": "不是列表"}))
    assert parsed == {"world_view": "x", "life_goals": []}


def test_parse_seed_rejects_garbage_and_empty():
    assert parse_seed("完全不是 JSON") is None
    assert parse_seed("") is None
    assert parse_seed("{}") is None
    assert parse_seed(json.dumps({"world_view": "  ", "life_goals": []})) is None
    assert parse_seed(json.dumps([1, 2, 3])) is None  # 数组而非对象


# ─── seed_perspective：拒绝分支 ─────────────────────────────────


def test_seed_disabled_without_store_or_engine():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, store, engine, client = _make(tmp, response=_seed_json())
        plugin._store = None
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "disabled"
        plugin._store = store
        plugin._engine = None
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "disabled"


def test_seed_disabled_when_promotion_off():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, _engine, client = _make(tmp, response=_seed_json(), enabled=False)
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "disabled"


def test_seed_disabled_when_seed_on_start_off():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, _engine, client = _make(
            tmp, response=_seed_json(), seed_on_start=False
        )
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "disabled"


def test_seed_no_anchor_when_identity_empty():
    """锚定层没配 → 无从派生（不是错误，用户可能还没填 [identity]）。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, _engine, client = _make(tmp, world="", values=(), response=_seed_json())
        with _PatchedClient(client):
            result = asyncio.run(seed_perspective(plugin, now=NOW))
        assert result["status"] == "no_anchor"
        assert client.calls == [], "无锚定层不得调 LLM"


def test_seed_failed_when_llm_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, engine, client = _make(tmp, fail=True)
        with _PatchedClient(client):
            result = asyncio.run(seed_perspective(plugin, now=NOW))
        assert result["status"] == "failed"
        assert engine.save_count == 0, "失败不写盘"
        assert engine.self_state["perspective"]["origin"] == ""


def test_seed_empty_when_unparsable_then_retries_next_start():
    """解析失败算 ``empty``（≠ failed），留空 + 下次启动会再试（未播种不被跳过）。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, engine, client = _make(tmp, response="胡说八道")
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "empty"
        assert engine.save_count == 0
        # 未播种 → 第二次仍是「可播种」状态（不会被 already 挡住）
        with _PatchedClient(_FakeClient(_seed_json())):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "ok"


# ─── seed_perspective：幂等 ─────────────────────────────────────


def test_seed_already_when_perspective_present():
    with tempfile.TemporaryDirectory() as tmp:
        state = default_self_state()
        state["perspective"]["world_view"] = "已有看法"
        plugin, _store, _engine, client = _make(tmp, state=state, response=_seed_json())
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "already"
        assert client.calls == [], "已播种不得重复调 LLM"


def test_seed_already_when_only_origin_set():
    """``origin`` 非空即视为已播种（即便两维为空也不覆盖——尊重既有写入者）。"""
    with tempfile.TemporaryDirectory() as tmp:
        state = default_self_state()
        state["perspective"]["origin"] = "manual"
        plugin, _store, _engine, client = _make(tmp, state=state, response=_seed_json())
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "already"


# ─── seed_perspective：成功路径 ─────────────────────────────────


def test_seed_ok_writes_state_and_marks_origin():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, engine, client = _make(tmp, response=_seed_json())
        with _PatchedClient(client):
            result = asyncio.run(seed_perspective(plugin, now=NOW))

        assert result["status"] == "ok"
        assert result["world_view"] == "她相信世界比想象的大"
        assert result["life_goals"] == ["学会游泳"]
        perspective = engine.self_state["perspective"]
        assert perspective["world_view"] == "她相信世界比想象的大"
        assert perspective["life_goals"] == ["学会游泳"]
        assert perspective["origin"] == "seed"
        assert perspective["updated_ts"] == NOW.isoformat(timespec="seconds")
        assert engine.save_count == 1


def test_seed_uses_low_temperature():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, _engine, client = _make(tmp, response=_seed_json())
        with _PatchedClient(client):
            asyncio.run(seed_perspective(plugin, now=NOW))
        assert client.calls[0]["temperature"] == _PROPOSAL.PROPOSAL_TEMPERATURE


def test_seed_does_not_touch_anchor_layer():
    """**只写慢变区**：seed 绝不改写 world_rules / values（锚定层只读）。"""
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, engine, client = _make(tmp, response=_seed_json())
        before = json.loads(json.dumps(engine.self_state))
        with _PatchedClient(client):
            asyncio.run(seed_perspective(plugin, now=NOW))
        # 除 perspective 段外，self state 其余部分完全不变
        after = dict(engine.self_state)
        after.pop("perspective")
        before.pop("perspective")
        assert after == before


def test_seed_is_idempotent_across_two_calls():
    with tempfile.TemporaryDirectory() as tmp:
        plugin, _store, engine, client = _make(tmp, response=_seed_json())
        with _PatchedClient(client):
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "ok"
            assert asyncio.run(seed_perspective(plugin, now=NOW))["status"] == "already"
        assert len(client.calls) == 1, "第二次不得再调 LLM"
        assert engine.save_count == 1


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
