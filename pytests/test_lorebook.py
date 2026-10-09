"""世界书 Lorebook loader 测试（v0.3.0 批 1 / R43 / grill 定案 2026-09-05）。

覆盖三层：
- **解析**：TOML → 条目模型全字段（缺省值、坏文件 fail-open 到空表）；
- **缓存**：mtime 变更热重载（读时校验，无看门狗——prework §1 定案）；
- **选取**：constant 常驻 → priority=high → 文件序的排序与 800 字预算截断；
  中文子串 / 英文 lowercase 触发；disabled / 无触发词的非常驻条目不进；
- **名册**：``list_cast()`` 只枚举 kind=cast 全量条目（批 2 播种器取材面）。

运行（项目根）：
    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_lorebook.py -q
"""

from __future__ import annotations

import os
import sys

import pytest

import _synth_loader

_LOADER = _synth_loader.load("services.lorebook.loader")

LorebookLoader = _LOADER.LorebookLoader
LorebookEntry = _LOADER.LorebookEntry


_Logger = _synth_loader.WarnLogger


_FULL_BOOK = """
[[entries]]
name = "临海市"
keys = ["临海", "海雾"]
content = "一座常年起海雾的沿海城市，老城区有电车。"
kind = "world"
constant = true
priority = "high"

[[entries]]
name = "面馆老板娘"
keys = ["面馆", "老板娘"]
content = "巷口面馆的老板娘，认得每一个常客的口味。"
kind = "cast"

[[entries]]
name = "旧书店"
keys = ["书店"]
content = "河堤旁的旧书店，店主养一只白猫。"
priority = "high"
"""

_EMPTY = ""


def _make(tmp_path, text, *, budget=800, max_entries=100):
    logger = _Logger()
    path = tmp_path / "lorebook.toml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    loader = LorebookLoader(path, budget=budget, max_entries=max_entries, logger=logger)
    return loader, logger, path


# ===== 解析 =====


def test_parse_full_fields(tmp_path):
    """TOML → 条目模型全字段透传。"""
    loader, _, _ = _make(tmp_path, _FULL_BOOK)
    entries = loader.entries()
    assert len(entries) == 3
    first = entries[0]
    assert isinstance(first, LorebookEntry)
    assert first.name == "临海市"
    assert first.keys == ("临海", "海雾")
    assert first.kind == "world"
    assert first.constant is True
    assert first.priority == "high"
    assert first.enabled is True


def test_parse_defaults(tmp_path):
    """可选字段缺省：kind=entry / constant=False / priority=low / enabled=True。"""
    loader, _, _ = _make(tmp_path, _FULL_BOOK)
    third = loader.entries()[2]
    assert third.kind == "entry"
    assert third.constant is False
    assert third.priority == "high"  # 本条显式给了 high
    assert third.enabled is True

    plain = '[[entries]]\nname = "甲"\ncontent = "内容"\n'
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    loader2, _, _ = _make(plain_dir, plain)
    entry = loader2.entries()[0]
    assert entry.kind == "entry"
    assert entry.constant is False
    assert entry.priority == "low"
    assert entry.enabled is True
    assert entry.keys == ()


def test_missing_file_is_empty_not_crash(tmp_path):
    """文件缺失 → 空表（fail-open 到「无世界书」，不炸注入主链路）。"""
    loader, logger, _ = _make(tmp_path, None)
    assert loader.entries() == []
    assert loader.select("随便说点什么") == []
    assert loader.list_cast() == []


def test_bad_toml_is_empty_with_warning(tmp_path):
    """解析失败 → 空表 + WARNING（用户手滑写崩不该拖垮主链路）。"""
    loader, logger, _ = _make(tmp_path, "这不是 TOML {{{")
    assert loader.entries() == []
    assert logger.warnings, "解析失败必须打 WARNING"


def test_mtime_hot_reload(tmp_path):
    """mtime 变更 → 下次读取拿到新内容（热重载，无需重启）。"""
    loader, _, path = _make(tmp_path, _EMPTY)
    assert loader.entries() == []

    path.write_text(_FULL_BOOK, encoding="utf-8")
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert len(loader.entries()) == 3


def test_max_entries_warns_but_keeps_all(tmp_path):
    """软上限：超出 max_entries 打 WARNING、不阻断、不静默截断（公开功能防手滑）。"""
    book = _FULL_BOOK + '\n[[entries]]\nname = "第四条"\ncontent = "多余条目"\n'
    loader, logger, _ = _make(tmp_path, book, max_entries=3)
    assert len(loader.entries()) == 4, "超限只告警不截断"
    assert logger.warnings


# ===== 选取（触发 / 排序 / 预算） =====


def test_select_constant_and_triggered(tmp_path):
    """constant 常驻无条件进；keys 子串命中才进；未命中不进。"""
    loader, _, _ = _make(tmp_path, _FULL_BOOK)
    picked = loader.select("今天路过那家面馆")
    names = [entry.name for entry in picked]
    assert "临海市" in names, "constant 条目不需要触发词"
    assert "面馆老板娘" in names, "触发词命中"
    assert "旧书店" not in names, "未命中不进"


def test_select_english_case_insensitive(tmp_path):
    """英文触发词 lowercase 子串匹配（prework §4）。"""
    book = '[[entries]]\nname = "Mond"\nkeys = ["Mondstadt"]\ncontent = "风之城。"\n'
    loader, _, _ = _make(tmp_path, book)
    assert [entry.name for entry in loader.select("i love mondstadt so much")] == ["Mond"]


def test_select_skips_disabled_and_keyless(tmp_path):
    """disabled 条目即使 constant 也不进；无触发词且非常驻的条目恒不触发。"""
    book = (
        '[[entries]]\nname = "关掉的常驻"\ncontent = "不该出现"\nconstant = true\nenabled = false\n'
        '[[entries]]\nname = "没词的普通条目"\ncontent = "无触发词"\n'
    )
    loader, _, _ = _make(tmp_path, book)
    assert loader.select("没词的普通条目 关掉的常驻") == []


def test_select_order_constant_high_fileorder(tmp_path):
    """排序：constant 优先 → priority=high → 文件序（超预算时先到先得）。"""
    book = (
        '[[entries]]\nname = "普通甲"\nkeys = ["目标"]\ncontent = "甲"\n'
        '[[entries]]\nname = "普通乙"\nkeys = ["目标"]\ncontent = "乙"\npriority = "high"\n'
        '[[entries]]\nname = "常驻"\ncontent = "常"\nconstant = true\n'
    )
    loader, _, _ = _make(tmp_path, book)
    names = [entry.name for entry in loader.select("目标")]
    assert names == ["常驻", "普通乙", "普通甲"]


def test_select_budget_truncates(tmp_path):
    """预算截断：按排序累计 content 长度，装不下的整条丢弃（不截半条）；
    跳过后继续尝试更短的候选（能填满预算就填）。"""
    book = (
        '[[entries]]\nname = "常驻"\ncontent = "' + "常" * 600 + '"\nconstant = true\n'
        '[[entries]]\nname = "命中乙"\nkeys = ["目标"]\ncontent = "' + "乙" * 400 + '"\n'
        '[[entries]]\nname = "命中丙"\nkeys = ["目标"]\ncontent = "' + "丙" * 100 + '"\n'
    )
    loader, _, _ = _make(tmp_path, book, budget=800)
    names = [entry.name for entry in loader.select("目标")]
    assert names == ["常驻", "命中丙"], "600 + 400 超 800 → 乙整条丢弃；丙更短、回填后装得下"


def test_select_budget_exact_boundary(tmp_path):
    """预算边界：恰好填满 800 的条目应被保留（≤ 语义）。"""
    book = (
        '[[entries]]\nname = "常驻"\ncontent = "' + "常" * 600 + '"\nconstant = true\n'
        '[[entries]]\nname = "正好"\nkeys = ["目标"]\ncontent = "' + "正" * 200 + '"\n'
    )
    loader, _, _ = _make(tmp_path, book, budget=800)
    names = [entry.name for entry in loader.select("目标")]
    assert names == ["常驻", "正好"]


# ===== 名册（批 2 播种器取材面） =====


def test_list_cast_enumerates_all_cast(tmp_path):
    """list_cast 只枚举 kind=cast 全量（不看触发词、不看 enabled 之外的任何条件）。"""
    loader, _, _ = _make(tmp_path, _FULL_BOOK)
    names = [entry.name for entry in loader.list_cast()]
    assert names == ["面馆老板娘"], "cast 名册 = 全量枚举，与 select 的触发逻辑无关"


if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
