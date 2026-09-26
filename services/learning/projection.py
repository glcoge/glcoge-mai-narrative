"""`[learned]` 区块读写层（批 3-C2 / ADR-0002 决策 6）。

**为什么不进 `config.py`**：一旦把 ``[learned]`` 声明成 pydantic 字段，它会出现在 WebUI
表单里；用户保存**任一**配置时，表单会以 **stale 值整体回写**，把插件写入的学习成果冲掉
（批 3 声明 → 批 4 写入 → WebUI 保存 = 完整事故链）。因此本区块**只存在于 ``config.toml``
原始文本**，SDK 不解析（``PluginConfigBase`` 是 ``extra="ignore"``）、WebUI 不渲染，
读写全部走本模块的 tomlkit 直通，展示走 ``/narrative status``。

**事实源在 db**：本区块只是**呈现层投影**，损坏可从 db 重建（ADR-0002 决策 6）。

写回用 tomlkit 增量语义——保留注释、保留旁段、只动传入的键（``tomlkit.dump`` 会整文件
丢注释，故不可用 ``Document`` 之外的朴素 dump 路径）。

**防重入**：写回自身 ``config.toml`` 可能触发宿主的 ``on_config_update(scope="self")``，
回调里若再写回即无限循环 → 写回期间置位自写标志，回调侧查 :func:`is_self_write_in_progress`
直接短路。批 3 只建机制（v1 不写），批 4 用。
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import tomlkit

#: ``config.toml`` 里的原始区块名（**不得**声明进 ``config.py``）
LEARNED_SECTION = "learned"

#: R1 预留槽：二期 LLM 风格提炼的投影（v1 恒空）  # RESERVED(R1)
STYLE_KEY = "style"

# 自写重入计数（嵌套安全；>0 即「当前处于插件自身写回中」）
_write_depth = 0


@contextlib.contextmanager
def learned_write_guard() -> Iterator[None]:
    """标记「插件正在写回自身 config.toml」的临界区。"""
    global _write_depth
    _write_depth += 1
    try:
        yield
    finally:
        _write_depth -= 1


def is_self_write_in_progress() -> bool:
    """当前是否处于插件自身的 ``config.toml`` 写回中（供 ``on_config_update`` 防重入）。"""
    return _write_depth > 0


def _warn(logger: Optional[Any], message: str, *args: Any) -> None:
    """容错分支必须留痕——静默失败会让「配了就没了」无从排查。"""
    if logger is not None:
        logger.warning(message, *args)


def _unwrap(value: Any) -> Any:
    """把 tomlkit 的容器值还原成普通 Python 值（标量本就是 str/int/bool 子类，直接用）。"""
    if isinstance(value, Mapping):
        return {str(key): _unwrap(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unwrap(item) for item in value]
    return value


def read_learned(path: Any, *, logger: Optional[Any] = None) -> Dict[str, Any]:
    """读取 ``[learned]`` 区块；缺文件 / 缺段 / 结构损坏一律返回 ``{}``（损坏时 WARN）。"""
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            document = tomlkit.load(handle)
    except Exception as exc:  # noqa: BLE001 —— 用户可手工编辑，语法错误不该让插件崩
        _warn(logger, "[learned] 解析 config.toml 失败，已按空处理: %s", exc)
        return {}
    section = document.get(LEARNED_SECTION)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        _warn(
            logger,
            "[learned] 区块应为表，实为 %s，已忽略",
            type(section).__name__,
        )
        return {}
    return {str(key): _unwrap(value) for key, value in section.items()}


def get_style_projection(path: Any, *, logger: Optional[Any] = None) -> List[str]:
    """读 R1 槽（``[learned].style``）；空 / 非列表 / 混入脏值都收敛为干净的字符串列表。"""
    raw = read_learned(path, logger=logger).get(STYLE_KEY)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        _warn(
            logger,
            "[learned].style 期望字符串列表，实为 %s，已忽略",
            type(raw).__name__,
        )
        return []
    items = [str(item) for item in raw if isinstance(item, str) and str(item)]
    if len(items) != len(raw):
        _warn(logger, "[learned].style 含非字符串或空项，已过滤 %d 项", len(raw) - len(items))
    return items


def _to_toml_value(value: Any) -> Any:
    """把普通 Python 值转成 tomlkit 可写回的形式。"""
    if isinstance(value, (list, tuple)):
        array = tomlkit.array()
        for item in value:
            array.append(_to_toml_value(item))
        return array
    if isinstance(value, Mapping):
        table = tomlkit.table()
        for key, item in value.items():
            table[str(key)] = _to_toml_value(item)
        return table
    return value


def write_learned(path: Any, data: Mapping[str, Any], *, logger: Optional[Any] = None) -> None:
    """把 ``data`` 增量合并进 ``[learned]`` 区块并写回（保留注释与旁段）。

    - 只覆盖 ``data`` 里出现的键，``[learned]`` 中已有的其他键**保持原样**；
    - 区块缺失时新建、追加到文件末尾；
    - 文件不存在时**抛** ``FileNotFoundError``（不静默新建半成品配置）。
    """
    del logger  # 写路径不吞异常：解析/权限错误直接抛出，让问题暴露
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config.toml 不存在，拒绝写回: {config_path}")
    with learned_write_guard():
        with open(config_path, "r", encoding="utf-8") as handle:
            document = tomlkit.load(handle)
        section = document.get(LEARNED_SECTION)
        if not isinstance(section, Mapping):
            section = tomlkit.table()
            document[LEARNED_SECTION] = section
        for key, value in data.items():
            section[str(key)] = _to_toml_value(value)
        with open(config_path, "w", encoding="utf-8") as handle:
            tomlkit.dump(document, handle)
