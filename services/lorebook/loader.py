"""世界书 loader：TOML 加载（mtime 缓存式热重载）+ 触发匹配 + 预算截断。

设计定案沿用 grill 收敛稿（2026-09-05 用户逐项拍板，`.scratch/narrative-persona/lorebook-prework.md`）：

- **单世界书单配置**（不做多书管理/切换）；条目存 data 目录 TOML，**不进 sqlite**。
- **mtime 热重载**（读时校验，无看门狗线程）：文件没变不重解析。
- **触发匹配零 LLM token**：中文子串 / 英文 lowercase 子串。
- **800 字预算**（默认，config 可调）：constant 优先 → priority=high → 文件序，
  累计超预算的条目**整条丢弃**（不截半条）。
- **100 条软上限**：超出打 WARNING 不阻断（公开功能防用户手滑写崩）。
- **解析失败 fail-open 到空表** + WARNING：坏文件只损失世界书注入，绝不拖垮
  注入主链路（与 hook 的 ``ErrorPolicy.SKIP`` 同哲学）。

字段契约（最小字段集，**只增不删**——字段即对外契约，prework §3）：

.. code-block:: toml

    [[entries]]
    name = "临海市"              # 条目名（用户自读备注 / NPC 名）
    keys = ["临海", "海雾"]      # 触发词（cast/world 可空）
    content = "……"               # 注入正文
    kind = "entry"               # entry | world | cast（缺省 entry；批 1 新增）
    constant = false             # true=每轮常驻注入
    priority = "low"             # high | low：命中超预算时 high 先进
    enabled = true

v0.3.0 修订（本批）：``kind`` 字段新增——``cast`` 即 NPC 名册（批 2 播种器
``list_cast()`` 取材面）；prework 原始形态（无 kind）条目按 ``entry`` 读取，
旧格式文件零迁移。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

__all__ = ["LorebookEntry", "LorebookLoader"]

#: 条目分型（v0.3.0 批 1 新增；world=世界设定 / cast=NPC 名册 / entry=普通关词条目）
KIND_ENTRY = "entry"
KIND_WORLD = "world"
KIND_CAST = "cast"
_VALID_KINDS = (KIND_ENTRY, KIND_WORLD, KIND_CAST)

#: priority 两档（prework §3 定案：砍掉 ST 的逐条插入深度/概率/冷却，只留高低）
PRIORITY_HIGH = "high"
PRIORITY_LOW = "low"

#: 单条目解析缺省值（缺字段不炸——TOML 手写容错）
_ENTRY_DEFAULTS = {
    "kind": KIND_ENTRY,
    "constant": False,
    "priority": PRIORITY_LOW,
    "enabled": True,
}


@dataclass(frozen=True)
class LorebookEntry:
    """一条世界书条目（字段即对外契约，只增不删）。"""

    name: str
    keys: Tuple[str, ...]
    content: str
    kind: str = KIND_ENTRY
    constant: bool = False
    priority: str = PRIORITY_LOW
    enabled: bool = True


class LorebookLoader:
    """世界书加载器：mtime 缓存 + 触发选取 + 预算截断（纯逻辑，零 engine 依赖）。"""

    def __init__(
        self,
        path: Any,
        budget: int,
        max_entries: int,
        logger: Any = None,
    ) -> None:
        # OBSERVE(R43)：本模块 = 世界书加载/触发/预算的单一实现（登记表 R43 行）。
        self._path = Path(path)
        self._budget = max(1, int(budget))
        self._max_entries = max(1, int(max_entries))
        self._logger = logger
        # mtime 缓存（None=还没读过）；文件缺失时也缓存 stat 失败态防反复打盘
        self._cache_mtime: Optional[float] = None
        self._cache_entries: List[LorebookEntry] = []

    # ─── 加载 ────────────────────────────────────────────────────

    def entries(self) -> List[LorebookEntry]:
        """全量条目（mtime 未变走缓存；文件缺失/解析失败 = 空表 + WARNING）。"""
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            # 缺文件缓存 -1：同一路径反复 missing 不重复告警
            if self._cache_mtime == -1.0:
                return []
            self._cache_mtime = -1.0
            self._cache_entries = []
            self._warn("世界书文件不存在: %s（视为空世界书）", self._path)
            return []

        if self._cache_mtime == mtime:
            return self._cache_entries

        raw = self._parse()
        if len(raw) > self._max_entries:
            # 软上限只告警不截断（prework §4）：截断会让用户以为条目生效了
            self._warn(
                "世界书条目数 %d 超过软上限 %d（全部保留，注意注入预算）",
                len(raw),
                self._max_entries,
            )
        self._cache_mtime = mtime
        self._cache_entries = raw
        return raw

    def _parse(self) -> List[LorebookEntry]:
        """TOML → 条目列表；解析失败 fail-open 到空表。"""
        try:
            with open(self._path, "rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            self._warn("世界书解析失败（视为空世界书）: %s", exc)
            return []

        entries: List[LorebookEntry] = []
        for index, raw in enumerate(data.get("entries", [])):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "") or "").strip() or f"条目{index + 1}"
            content = str(raw.get("content", "") or "").strip()
            if not content:
                continue  # 空正文条目无注入意义，静默跳过
            keys = tuple(
                str(key).strip()
                for key in (raw.get("keys", []) or [])
                if str(key).strip()
            )
            kind = str(raw.get("kind", "") or "").strip() or _ENTRY_DEFAULTS["kind"]
            if kind not in _VALID_KINDS:
                self._warn("世界书条目 %r 的 kind=%r 非法（按 entry 处理）", name, kind)
                kind = KIND_ENTRY
            priority = str(raw.get("priority", "") or "").strip() or _ENTRY_DEFAULTS["priority"]
            entries.append(
                LorebookEntry(
                    name=name,
                    keys=keys,
                    content=content,
                    kind=kind,
                    constant=bool(raw.get("constant", _ENTRY_DEFAULTS["constant"])),
                    priority=priority if priority in (PRIORITY_HIGH, PRIORITY_LOW) else PRIORITY_LOW,
                    enabled=bool(raw.get("enabled", _ENTRY_DEFAULTS["enabled"])),
                )
            )
        return entries

    # ─── 选取 ────────────────────────────────────────────────────

    def select(self, recent_text: str) -> List[LorebookEntry]:
        """触发选取：constant 全取 + keys 子串命中，按预算截断（排序见模块文档）。

        Args:
            recent_text: 触发扫描输入（本轮 + 最近 2~3 轮对话文本，零 LLM token）。
        """
        haystack = str(recent_text or "").lower()
        candidates: List[Tuple[int, int, int, LorebookEntry]] = []
        for index, entry in enumerate(self.entries()):
            if not entry.enabled:
                continue
            if entry.constant:
                rank_constant = 0
            elif haystack and any(key.lower() in haystack for key in entry.keys):
                rank_constant = 1
            else:
                continue
            rank_priority = 0 if entry.priority == PRIORITY_HIGH else 1
            candidates.append((rank_constant, rank_priority, index, entry))

        picked: List[LorebookEntry] = []
        used = 0
        for _, _, _, entry in sorted(candidates, key=lambda item: item[:3]):
            if used + len(entry.content) > self._budget:
                continue  # 整条丢弃不截半条；低优先级候选仍可能更短、装得下
            picked.append(entry)
            used += len(entry.content)
        return picked

    def list_cast(self) -> List[LorebookEntry]:
        """NPC 名册全量枚举（kind=cast 且 enabled）——批 2 播种器的取材面。"""
        return [entry for entry in self.entries() if entry.kind == KIND_CAST and entry.enabled]

    # ─── 内部 ────────────────────────────────────────────────────

    def _warn(self, message: str, *args: Any) -> None:
        if self._logger is not None:
            self._logger.warning(message, *args)
