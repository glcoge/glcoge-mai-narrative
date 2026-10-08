"""引擎依赖束（深化 B / grilling Q1a）：模块函数吃 ``deps`` 而非 ``engine``。

字段 = services 实际用到的全部子系统句柄（2026-10-08 全量侦察定案，11 个）——
让 services 不再伸手 engine 私有字段（私有字段引用），
也让「一个模块需要什么」在签名上一眼可读。

装配纪律（三处，全部显式）：
- **构造装配**：``NarrativeEngine.__init__`` 建首个快照。此时 ``on_load`` 还没建
  ``_streams`` / ``_lorebook`` 等，可选字段可能为 None——**属正常中间态**；
- **完成装配**：``on_load`` 尾部 ``rebind_deps()`` 补全（必须在 ``_reconcile_all``
  之前：tick 一启动就会读 deps）；
- **热重载重绑定**：宿主每次热重载**换新配置实例**（SDK ``set_plugin_config``
  重新 validate；runner ``_handle_config_updated`` 先注入新配置、后回调
  ``on_config_update``），故回调里必须 ``rebind_deps()``——不重绑定则 tick /
  reconcile 永远读旧配置（启停判定失真）。

Deps 是**冻结快照**：长持有的子系统（B1 起 CreatorClient、B2 起 scheduler、
B3 起 promotion 等）在转换时各自获得 ``rebind_deps``，由装配方在配置变更点
级联刷新；字段只增不删。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

__all__ = ["Deps"]


@dataclass(frozen=True)
class Deps:
    """一次装配的依赖快照（``engine.deps``；字段语义见模块 docstring）。"""

    config: Any  # plugin.config（快照——热重载经 rebind_deps 换新）
    store: Any  # NarrativeStore / FakeStore（on_load 建一次，身份稳定）
    logger: Any  # ctx.logger
    local_now: Callable[[], datetime]  # 剧本时区本地时间（读快照 config 的时区偏移）

    # 状态门面：engine 自身（或测试桩）——消费其 load/save_self_state、
    # load/save_branch_state、is_asleep、_sleep_configured。状态读写是 engine
    # 本职（Q5a 保留清单），services 经本句柄调用而非绕过（测试桩手挂的
    # 实例属性 lambda 因此天然生效，旧桩零改动）。
    state: Any = None
    creator: Any = None  # 创作 LLM 客户端（generate(prompt)）
    telemetry: Any = None  # 可选子系统：缺省 None ≡ 现行 getattr 宽容语义
    lorebook: Any = None  # 可选子系统：enabled=false 时不装配
    streams: Any = None  # uid↔stream 注册表（seeder 拦截词表读 known_uids）
    group_streams: Any = None  # gid↔session 注册表（同上 known_gids）
    native_config_get: Any = None  # 主程序配置读口（ctx.config.get，async）