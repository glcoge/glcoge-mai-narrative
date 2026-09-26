"""`[learned]` 区块读写层（批 3-C2 / ADR-0002 决策 6）——tomlkit 直读写 + 防重入。

背景（ADR-0002 决策 6）：`[learned]` **不声明进 config.py**（WebUI 不渲染即不回写，
规避 stale 覆盖）；只落 ``config.toml`` 原始区块，用 tomlkit 增量写回，
展示走 ``/narrative status``。

❗ 事故链（当初预判并规避的）：批 3 声明进 config.py → 批 4 tomlkit 写学习成果
→ 用户在 WebUI 保存任一配置 → 表单以 **stale 值整体回写** → 学习成果被冲掉。

本用例锁的是**读写层契约**：缺段容错、非破坏性写回（保注释/保旁段）、
R1 槽（``style``）空即空、以及 ``on_config_update`` 防重入用的自写标志。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from pytests._synth_loader import load  # noqa: E402

_PROJ = load("services.learning.projection")

LEARNED_SECTION = _PROJ.LEARNED_SECTION
STYLE_KEY = _PROJ.STYLE_KEY
read_learned = _PROJ.read_learned
write_learned = _PROJ.write_learned
get_style_projection = _PROJ.get_style_projection
is_self_write_in_progress = _PROJ.is_self_write_in_progress
learned_write_guard = _PROJ.learned_write_guard


class _Recorder:
    """记录 WARN 的假 logger（容错分支必须留痕，不能静默）。"""

    def __init__(self) -> None:
        self.warnings: list = []

    def warning(self, *args, **kwargs) -> None:
        self.warnings.append(args)

    def info(self, *args, **kwargs) -> None:
        pass

    def debug(self, *args, **kwargs) -> None:
        pass

    def error(self, *args, **kwargs) -> None:
        pass


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


def _write_toml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ─── 读：缺文件 / 缺段 / 正常 ────────────────────────────────


def test_read_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_learned(tmp_path / "nope.toml") == {}


def test_read_without_section_returns_empty(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "[plugin]\nenabled = true\n")
    assert read_learned(path) == {}


def test_read_returns_section_content(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, '[learned]\nstyle = ["句子偏短"]\n')
    data = read_learned(path)
    assert data[STYLE_KEY] == ["句子偏短"]


def test_read_tolerates_non_table_section(tmp_path: Path) -> None:
    """[learned] 被写坏成标量 → 返回空 + WARN，不让插件崩。"""
    path = _write_toml(tmp_path, 'learned = "oops"\n')
    logger = _Recorder()
    assert read_learned(path, logger=logger) == {}
    assert logger.warnings, "结构损坏必须留下 WARN，不能静默"


def test_read_broken_toml_returns_empty(tmp_path: Path) -> None:
    """人工编辑出语法错误 → 返回空 + WARN。"""
    path = _write_toml(tmp_path, "[learned]\nstyle = [\n")
    logger = _Recorder()
    assert read_learned(path, logger=logger) == {}
    assert logger.warnings


# ─── R1 槽：style 投影 ──────────────────────────────────────


def test_style_projection_empty_by_default(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "[learned]\n")
    assert get_style_projection(path) == []


def test_style_projection_reads_string_list(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, '[learned]\nstyle = ["偏短", "少用感叹号"]\n')
    assert get_style_projection(path) == ["偏短", "少用感叹号"]


def test_style_projection_tolerates_non_list(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, '[learned]\nstyle = "偏短"\n')
    logger = _Recorder()
    assert get_style_projection(path, logger=logger) == []
    assert logger.warnings


def test_style_projection_filters_non_string_items(tmp_path: Path) -> None:
    """列表里混入非字符串 → 只保留字符串项 + WARN（不外泄脏值）。"""
    path = _write_toml(tmp_path, '[learned]\nstyle = ["偏短", 42, ""]\n')
    logger = _Recorder()
    assert get_style_projection(path, logger=logger) == ["偏短"]
    assert logger.warnings


# ─── 写：非破坏性增量写回 ───────────────────────────────────


def test_write_creates_section_when_absent(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, "[plugin]\nenabled = true\n")
    write_learned(path, {STYLE_KEY: ["偏短"]})
    assert get_style_projection(path) == ["偏短"]


def test_write_preserves_other_sections(tmp_path: Path) -> None:
    path = _write_toml(tmp_path, SAMPLE)
    write_learned(path, {STYLE_KEY: ["偏短"]})
    text = path.read_text(encoding="utf-8")
    assert 'world = "某个世界观"' in text
    assert "诚实" in text
    assert 'enabled = true' in text


def test_write_preserves_comments(tmp_path: Path) -> None:
    """tomlkit 的核心价值：写回不吞注释（普通 dump 会全丢）。"""
    path = _write_toml(tmp_path, SAMPLE)
    write_learned(path, {STYLE_KEY: ["偏短"]})
    text = path.read_text(encoding="utf-8")
    assert "顶部注释（不能被写回破坏）" in text
    assert "锚定层注释" in text


def test_write_merges_into_existing_section(tmp_path: Path) -> None:
    """写回只覆盖传入键，不抹掉 [learned] 里已有的其他键。"""
    path = _write_toml(tmp_path, '[learned]\nnote = "保留我"\nstyle = []\n')
    write_learned(path, {STYLE_KEY: ["偏短"]})
    data = read_learned(path)
    assert data["note"] == "保留我"
    assert data[STYLE_KEY] == ["偏短"]


def test_write_missing_file_raises(tmp_path: Path) -> None:
    """文件不存在时**报错**而不是静默新建（避免在错误路径造出半成品配置）。"""
    with pytest.raises(FileNotFoundError):
        write_learned(tmp_path / "nope.toml", {STYLE_KEY: ["x"]})


# ─── 防重入：自写标志 ───────────────────────────────────────


def test_flag_clear_by_default() -> None:
    assert is_self_write_in_progress() is False


def test_guard_sets_and_clears_flag() -> None:
    with learned_write_guard():
        assert is_self_write_in_progress() is True
    assert is_self_write_in_progress() is False


def test_guard_is_nesting_safe() -> None:
    """嵌套时内层退出不能提前清标志（计数而非布尔）。"""
    with learned_write_guard():
        with learned_write_guard():
            assert is_self_write_in_progress() is True
        assert is_self_write_in_progress() is True
    assert is_self_write_in_progress() is False


def test_guard_resets_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with learned_write_guard():
            raise RuntimeError("boom")
    assert is_self_write_in_progress() is False


def test_write_auto_wraps_guard(tmp_path: Path) -> None:
    """write_learned 自身套 guard：调用方事后查标志必为 False。"""
    path = _write_toml(tmp_path, "[plugin]\nenabled = true\n")
    write_learned(path, {STYLE_KEY: ["偏短"]})
    assert is_self_write_in_progress() is False


if __name__ == "__main__":
    from pytests._synth_loader import run_standalone

    raise SystemExit(run_standalone(globals()))
