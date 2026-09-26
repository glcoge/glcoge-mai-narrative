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

from ..state.continuity import SLOW_FIELD_AUDIENCE, slow_get

#: ``config.toml`` 里的原始区块名（**不得**声明进 ``config.py``）
LEARNED_SECTION = "learned"

#: R1 预留槽：二期 LLM 风格提炼的投影（v1 恒空）  # RESERVED(R1)
STYLE_KEY = "style"

#: 受众标识：只有 ``general`` 维度的现值才允许落 ``config.toml``（批 4-C6 / E5 裁定）。
GENERAL_AUDIENCE = "general"

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


def _merge_learned(
    path: Any,
    data: Mapping[str, Any],
    *,
    remove: Optional[List[str]] = None,
) -> None:
    """``[learned]`` 增量合并的公共实现（``write_learned`` / ``rebuild_projection`` 共用）。

    - 只覆盖 ``data`` 里出现的键，``[learned]`` 中已有其他键保持原样；
    - ``remove`` 里的键**先删后写**（重建语义：db 已清空的维度，投影不得留陈旧值）；
    - 区块缺失时新建并追加到文件末尾；
    - 文件不存在时**抛** ``FileNotFoundError``（不静默新建半成品配置）。
    """
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
        for key in remove or []:
            if str(key) in section:
                del section[str(key)]
        for key, value in data.items():
            section[str(key)] = _to_toml_value(value)
        with open(config_path, "w", encoding="utf-8") as handle:
            tomlkit.dump(document, handle)


def write_learned(path: Any, data: Mapping[str, Any], *, logger: Optional[Any] = None) -> None:
    """把 ``data`` 增量合并进 ``[learned]`` 区块并写回（保留注释与旁段）。

    只加不删——删除语义只在 :func:`rebuild_projection`。
    """
    del logger  # 写路径不吞异常：解析/权限错误直接抛出，让问题暴露
    _merge_learned(path, data)


# ─── 慢变现值 → [learned] 投影（批 4-C6） ───────────────────────


def projected_paths() -> List[str]:
    """可投影到 ``config.toml`` 的慢变维度＝受众表里所有 ``general`` 项。

    **刻意自动派生，不手写白名单**：将来新增 per_user 维度（如 relationship 家族）
    会因受众是 ``per_user`` 被自动排除，不可能因「忘了同步投影名单」而泄露。
    这是 fail-closed 在投影侧的同一口径（E5：只投影 general）。
    """
    return [
        path for path, audience in SLOW_FIELD_AUDIENCE.items() if audience == GENERAL_AUDIENCE
    ]


def _leaf(path: str) -> str:
    """``perspective.world_view`` → ``world_view``（``[learned]`` 里用扁平键）。"""
    return str(path).rsplit(".", 1)[-1]


def build_projection(state: Mapping[str, Any]) -> Dict[str, Any]:
    """从自我层 state 抽出可投影的 general 慢变现值（纯函数，不碰磁盘）。

    空值（``""`` / ``[]`` / None）**跳过**：投影是「给她看的当前看法」，
    写一条空值只会把已有内容抹掉，没有任何信息量。
    """
    data: Dict[str, Any] = {}
    for path in projected_paths():
        value = slow_get(dict(state), path)
        if value is None or value == "" or value == []:
            continue
        data[_leaf(path)] = value
    return data


def write_projection(
    path: Any,
    state: Mapping[str, Any],
    *,
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """把 general 慢变现值**增量**写回 ``[learned]``（晋升 / seed 成功后调用）。

    返回实际写入的键值（便于调用方日志与断言）。只加不删：一次失败的重建
    不该把已写好的投影抹掉。
    """
    data = build_projection(state)
    if not data:
        return {}
    write_learned(path, data, logger=logger)
    return data


def rebuild_projection(
    path: Any,
    state: Mapping[str, Any],
    *,
    logger: Optional[Any] = None,
) -> Dict[str, Any]:
    """从事实源（db 现值）**权威重建** ``[learned]`` 投影。

    与 :func:`write_projection` 的差别＝**会删**：db 里已清空的 general 维度，
    投影中的陈旧键会被一并移除——用于回滚（C8）与人工修复「投影与 db 不一致」。

    ⚠️ ``remove`` 只覆盖 ``projected_paths()``（general 白名单），**不会**碰
    ``[learned]`` 里的其它键（如 R1 的 ``style`` 槽与用户手写内容）。
    """
    del logger
    data = build_projection(state)
    remove = [_leaf(item) for item in projected_paths() if _leaf(item) not in data]
    _merge_learned(path, data, remove=remove)
    return data
