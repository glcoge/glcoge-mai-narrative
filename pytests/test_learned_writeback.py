"""`[learned]` 投影写回（v0.2.0 批 4 · C6）：general 慢变现值 → config.toml。

批 3 只建了 ``[learned]`` 的读写层（v1 不写）；本单元接上**写入端**：晋升 / seed
成功后把当前看法投影回配置，让人能直接在 config.toml 里看见「她现在的看法」。

三条硬纪律：

- **E5：只投影 general 维度**（``world_view`` / ``life_goals``）。
- ❗ **per_user 零落盘**：relationship 四维写进 config.toml 即跨用户可见 = 隐私事故，
  本文件用「状态里塞满 relationship 值 → 磁盘上找不到任何 per_user 键」来锁死。
- **投影可重建**：事实源在 db，``rebuild_projection`` 会**删掉** db 已清空的陈旧键。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_learned_writeback.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_learned_writeback.py
"""

from __future__ import annotations

import types
from pathlib import Path

import _synth_loader

_PROJ = _synth_loader.load("services.learning.projection")
_CONT = _synth_loader.load("services.state.continuity")
_ENGINE = _synth_loader.load("services.state.engine")
# 真正执行 services/__init__.py（plugin.py 依赖其再导出），子模块走标准导入机制
_synth_loader.load("services")
_PLUGIN = _synth_loader.load("plugin")

build_projection = _PROJ.build_projection
projected_paths = _PROJ.projected_paths
write_projection = _PROJ.write_projection
rebuild_projection = _PROJ.rebuild_projection
read_learned = _PROJ.read_learned
STYLE_KEY = _PROJ.STYLE_KEY
default_self_state = _ENGINE.default_self_state
MaiNarrativePlugin = _PLUGIN.MaiNarrativePlugin


SAMPLE = """\
# 顶部注释（不能被写回破坏）
[plugin]
enabled = true

# 锚定层注释
[identity]
world = "某个世界观"
values = ["诚实"]

[learned]
# v1 恒空：二期 LLM 风格提炼的投影槽（R1）
style = []
"""


def _write_toml(tmp_path: Path, text: str = SAMPLE) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _state(world_view="她相信世界比想象的大", goals=("学会游泳",)):
    state = default_self_state()
    state["perspective"]["world_view"] = world_view
    state["perspective"]["life_goals"] = list(goals)
    return state


# ─── 白名单派生（E5） ───────────────────────────────────────────


def test_projected_paths_are_exactly_the_general_ones():
    """不是手写白名单，而是从受众表**派生**：general 维度全在、per_user 全不在。"""
    assert projected_paths() == ["perspective.world_view", "perspective.life_goals"]


def test_projected_paths_exclude_relationship():
    """**强约束的投影侧**：relationship 四维一个都不许投影（审计留痕）。"""
    projected = projected_paths()
    for path in ("relationship.trust", "relationship.closeness",
                 "relationship.boundaries", "relationship.stage"):
        assert path not in projected
        assert path in _CONT.SLOW_FIELD_AUDIENCE  # 反向确认：它确实登记在受众表里


def test_projected_paths_track_audience_table():
    """派生关系的**结构性断言**：投影集合恒等于受众表里 general 的子集。

    将来有人新增 general 维度却忘了同步投影，这条会红——比手写白名单可靠。
    """
    general = {
        path
        for path, audience in _CONT.SLOW_FIELD_AUDIENCE.items()
        if audience == _PROJ.GENERAL_AUDIENCE
    }
    assert set(projected_paths()) == general


# ─── build_projection（纯函数） ─────────────────────────────────


def test_build_projection_picks_general_values():
    data = build_projection(_state())
    assert data == {"world_view": "她相信世界比想象的大", "life_goals": ["学会游泳"]}


def test_build_projection_skips_empty_values():
    """空值跳过——写一条空值只会抹掉已有投影，没有信息量。"""
    assert build_projection(default_self_state()) == {}
    assert build_projection(_state(world_view="", goals=())) == {}


def test_build_projection_never_reads_relationship_sections():
    """自我层里就算混进 relationship 段，投影也只取 general 两维。"""
    state = _state()
    state["relationship"] = {"trust": 0.9, "closeness": 0.9, "boundaries": 0.1, "stage": "很亲近"}
    assert set(build_projection(state)) == {"world_view", "life_goals"}


# ─── 写回 ───────────────────────────────────────────────────────


def test_write_projection_writes_values(tmp_path: Path):
    path = _write_toml(tmp_path)
    written = write_projection(path, _state())
    assert written == {"world_view": "她相信世界比想象的大", "life_goals": ["学会游泳"]}
    data = read_learned(path)
    assert data["world_view"] == "她相信世界比想象的大"
    assert data["life_goals"] == ["学会游泳"]


def test_write_projection_preserves_comments_and_other_keys(tmp_path: Path):
    """增量写回保注释、保旁段、保 R1 槽（tomlkit 的核心价值）。"""
    path = _write_toml(tmp_path)
    write_projection(path, _state())
    text = path.read_text(encoding="utf-8")
    assert "# 顶部注释（不能被写回破坏）" in text
    assert "# v1 恒空：二期 LLM 风格提炼的投影槽（R1）" in text
    assert "world = \"某个世界观\"" in text
    assert read_learned(path)[STYLE_KEY] == []


def test_write_projection_skips_when_nothing_to_write(tmp_path: Path):
    """没有可写的 general 值 → 不碰文件（返回空 dict 供调用方判断）。"""
    path = _write_toml(tmp_path)
    before = path.read_text(encoding="utf-8")
    assert write_projection(path, default_self_state()) == {}
    assert path.read_text(encoding="utf-8") == before


def test_write_projection_does_not_delete_existing_projection(tmp_path: Path):
    """**只加不删**：一次空状态不该把已写好的投影抹掉（防「失败的重建」毁数据）。"""
    path = _write_toml(tmp_path)
    write_projection(path, _state())
    write_projection(path, default_self_state())  # 空状态
    assert read_learned(path)["world_view"] == "她相信世界比想象的大"


def test_write_projection_missing_file_raises(tmp_path: Path):
    """不静默新建半成品配置（与批 3 write_learned 同口径）。"""
    try:
        write_projection(tmp_path / "nope.toml", _state())
    except FileNotFoundError:
        pass
    else:  # pragma: no cover
        raise AssertionError("缺文件时必须抛 FileNotFoundError")


# ─── per_user 零落盘（❗ 隐私红线） ─────────────────────────────


def test_relationship_never_reaches_disk(tmp_path: Path):
    """**E5 / 隐私红线**：per_user 值绝不进 config.toml。

    构造一个「什么都塞了」的自我层状态，写回后逐个 grep 磁盘文本：
    config.toml 里不得出现任何 relationship 键名或它的值。
    """
    path = _write_toml(tmp_path)
    state = _state()
    state["relationship"] = {
        "trust": 0.87, "closeness": 0.91, "boundaries": 0.13, "stage": "很亲近",
    }
    write_projection(path, state)
    text = path.read_text(encoding="utf-8")
    for token in ("trust", "closeness", "boundaries", "stage", "0.87", "0.91", "0.13", "很亲近"):
        assert token not in text, f"per_user 值泄露到 config.toml: {token}"

    # 反向确认：general 值**确实**写进去了（否则上面的断言可能只是「什么都没写」）
    data = read_learned(path)
    assert data["world_view"] == "她相信世界比想象的大"


def test_rebuild_projection_also_excludes_relationship(tmp_path: Path):
    path = _write_toml(tmp_path)
    state = _state()
    state["relationship"] = {"trust": 0.87, "closeness": 0.91}
    rebuild_projection(path, state)
    text = path.read_text(encoding="utf-8")
    assert "trust" not in text and "0.87" not in text
    assert read_learned(path)["world_view"] == "她相信世界比想象的大"


# ─── 重建语义（会删） ───────────────────────────────────────────


def test_rebuild_projection_removes_stale_keys(tmp_path: Path):
    """db 已清空的维度，投影里的陈旧键必须被删（否则投影与事实源长期不一致）。"""
    path = _write_toml(tmp_path)
    write_projection(path, _state())
    assert read_learned(path).get("life_goals") == ["学会游泳"]

    rebuild_projection(path, _state(goals=()))  # db 里 goals 清空
    data = read_learned(path)
    assert "life_goals" not in data, "清空的维度必须从投影移除"
    assert data["world_view"] == "她相信世界比想象的大"  # 未清空的保留


def test_rebuild_projection_keeps_unrelated_and_style_slot(tmp_path: Path):
    """重建只动 general 白名单内的键，绝不清扫 ``[learned]`` 里的其它内容。"""
    path = _write_toml(tmp_path)
    write_projection(path, _state())
    rebuild_projection(path, default_self_state())  # 全空 → 两个键都该被删
    data = read_learned(path)
    assert "world_view" not in data and "life_goals" not in data
    assert data[STYLE_KEY] == []  # R1 槽不受影响
    text = path.read_text(encoding="utf-8")
    assert "# 顶部注释（不能被写回破坏）" in text


# ─── plugin 接线 ────────────────────────────────────────────────


class _WarnRecorder:
    def __init__(self):
        self.warnings: list = []
        self.debugs: list = []

    def warning(self, *args, **kwargs):
        self.warnings.append(args)

    def debug(self, *args, **kwargs):
        self.debugs.append(args)

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def _fake_plugin(path: Path, state, logger=None):
    return types.SimpleNamespace(
        _engine=types.SimpleNamespace(load_self_state=lambda: state),
        _config_path=lambda: path,
        ctx=types.SimpleNamespace(logger=logger or _WarnRecorder()),
    )


def test_plugin_project_learned_writes(tmp_path: Path):
    path = _write_toml(tmp_path)
    fake = _fake_plugin(path, _state())
    MaiNarrativePlugin._project_learned(fake)
    assert read_learned(path)["world_view"] == "她相信世界比想象的大"


def test_plugin_project_learned_swallows_failure(tmp_path: Path):
    """写回失败**不得**冒泡打断看门狗（投影是附属品，db 才是事实源）。"""
    recorder = _WarnRecorder()
    fake = _fake_plugin(tmp_path / "nope.toml", _state(), logger=recorder)
    MaiNarrativePlugin._project_learned(fake)  # 不抛
    assert recorder.warnings, "失败必须 WARN 留痕，不得静默"


def test_plugin_project_learned_noop_without_engine():
    """无引擎（未启用）时不写、不炸。"""
    fake = types.SimpleNamespace(
        _engine=None,
        _config_path=lambda: Path("nonexistent.toml"),
        ctx=types.SimpleNamespace(logger=_WarnRecorder()),
    )
    MaiNarrativePlugin._project_learned(fake)  # 不抛


if __name__ == "__main__":
    raise SystemExit(_synth_loader.run_standalone(globals()))
