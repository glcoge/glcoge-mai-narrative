"""narrative 测试共享合成包 loader（四份测试文件此前各自内嵌同一套实现）。

插件目录名含连字符（``glcoge-mai-narrative``）不能直接作为包名 import——
借鉴 mai-diary 的做法，用 ``importlib`` 挂到合成包名下加载。此前
test_plugin_hooks / test_engine_rules / test_render_turn / test_creator_route
各自复制同一套 loader 与独立运行入口（重复约 120 行），2026-09-13 体检（C3）
收敛到此单点：loader 需要修时只改这一处。
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SYNTH_PKG = "_narrative_test_plugin"


def _install_synth_package() -> None:
    """注册合成包（根 + services），使插件模块的相对导入可解析。"""
    if _SYNTH_PKG in sys.modules:
        return
    root = types.ModuleType(_SYNTH_PKG)
    root.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    sys.modules[_SYNTH_PKG] = root
    services = types.ModuleType(f"{_SYNTH_PKG}.services")
    services.__path__ = [str(PLUGIN_ROOT / "services")]  # type: ignore[attr-defined]
    sys.modules[f"{_SYNTH_PKG}.services"] = services


def _register_synth_package(full_name: str, pkg_dir: Path) -> None:
    """注册合成父包：有 ``__init__.py`` 的**真包**执行其 init（保持导出面），
    无 init 的目录才造空壳。

    空壳会顶掉真包——2026-10-07 批 2 实证：先 ``load("services.lorebook.loader")``
    把 ``services.lorebook`` 注册成空壳，随后 ``services/__init__`` 的
    ``from .lorebook import LorebookLoader`` 拿到空壳直接 ImportError
    （全量字母序 b<s 恰好绕开，命令行顺序一变就炸）。
    """
    if full_name in sys.modules:
        return
    init_file = pkg_dir / "__init__.py"
    if not init_file.exists():
        pkg_mod = types.ModuleType(full_name)
        pkg_mod.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
        sys.modules[full_name] = pkg_mod
        return
    spec = importlib.util.spec_from_file_location(full_name, str(init_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {full_name} from {init_file}")
    pkg_mod = importlib.util.module_from_spec(spec)
    pkg_mod.__path__ = [str(pkg_dir)]  # type: ignore[attr-defined]
    sys.modules[full_name] = pkg_mod
    spec.loader.exec_module(pkg_mod)


def load(rel_name: str) -> types.ModuleType:
    """按相对名加载插件模块（如 ``"services.state.engine"`` / ``"plugin"``）。

    命中目录时回退加载其 ``__init__.py``（如 ``"services"`` → services/__init__.py，
    真正执行再导出，plugin.py 依赖它）。
    """
    _install_synth_package()
    parts = rel_name.split(".")
    # 逐级注册合成子包（v0.2.0 批 0：services 目录化后出现子包层级，
    # 模块内相对导入要求所有父包在 sys.modules 中可解析）
    for depth in range(2, len(parts)):
        pkg_full = f"{_SYNTH_PKG}." + ".".join(parts[:depth])
        _register_synth_package(pkg_full, PLUGIN_ROOT.joinpath(*parts[:depth]))
    file_path = PLUGIN_ROOT.joinpath(*parts)
    if file_path.is_dir():
        file_path = file_path / "__init__.py"
    else:
        file_path = file_path.with_suffix(".py")
    full_name = f"{_SYNTH_PKG}.{rel_name}"
    spec = importlib.util.spec_from_file_location(full_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {full_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def null_logger() -> types.SimpleNamespace:
    """静默 logger（v0.1.10）：睡眠状态机在入睡/醒来/被吵醒时会打 INFO，
    测试里的假 plugin 必须提供 ctx.logger，否则转换瞬间 AttributeError。
    """
    return types.SimpleNamespace(
        info=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )


# [narrative] 睡眠态字段的出厂值（与 config.py 的 Field default 保持一致）。
# 单个用例要改某项时传关键字覆盖；要关闭睡眠态传 sleep_time=""。
SLEEP_DEFAULTS = {
    "sleep_time": "23:30",
    "wake_time": "07:00",
    "sleep_delay_max_minutes": 60,
    "sleep_delay_recent_minutes": 10,
    "woken_awake_minutes": 30,
    "energy_woken_penalty": 0.08,
    "energy_woken_floor": 0.3,
    "wake_fragment_enabled": True,
    "sleep_pre_sleep_hint_minutes": 25,
}


def sleep_config(**overrides) -> types.SimpleNamespace:
    """睡眠态配置夹具：默认全开，传 ``sleep_time=""`` 即关闭整个睡眠态。"""
    merged = dict(SLEEP_DEFAULTS)
    merged.update(overrides)
    return types.SimpleNamespace(**merged)


# [promotion] 出厂值（与 config.py 的 Field default 保持一致）。
# 单个用例要改某项时传关键字覆盖；要关闭整机传 enabled=False。
PROMOTION_DEFAULTS = {
    "enabled": True,
    "seed_on_start": True,
    "interval_hours": 168,
    "failure_backoff_hours": 6,
    "min_evidence_entries": 5,
    "max_proposals": 8,
    "minor_confidence": 0.82,
    "minor_min_scenes": 3,
    "minor_min_days": 2,
    "cooldown_hours": 72,
    "major_enabled": False,
    "major_confidence": 0.95,
    "major_min_scenes": 2,
    "refutation_penalty": 0.2,
    "merge_bonus": 0.05,
    "relation_min_days": 3,
    "relation_days_per_step": 2,
    "relation_step": 0.05,
    "relation_max": 0.8,
    "projection_limit": 2,
}


def promotion_config(**overrides) -> types.SimpleNamespace:
    """慢变晋升机配置夹具（默认与 config.py 出厂值一致）。"""
    merged = dict(PROMOTION_DEFAULTS)
    merged.update(overrides)
    return types.SimpleNamespace(**merged)


# [lorebook] 出厂值（v0.3.0 批 1 / R43，与 config.py 的 Field default 保持一致）。
# enabled 默认 False：既有测试夹具不带本段也零影响（planner_block 对缺段按关闭处理）。
LOREBOOK_DEFAULTS = {
    "enabled": False,
    "mode": "simple",
    "file": "lorebook.toml",
    "inject_budget_chars": 800,
    "max_entries": 100,
}


def lorebook_config(**overrides) -> types.SimpleNamespace:
    """世界书配置夹具（默认与 config.py 出厂值一致，enabled=False 零行为）。"""
    merged = dict(LOREBOOK_DEFAULTS)
    merged.update(overrides)
    return types.SimpleNamespace(**merged)


# [seeder] 出厂值（v0.3.0 批 2 / R40，与 config.py 的 Field default 保持一致）。
# enabled 默认 False：播种器完全关闭（零行为），观察期起步量级见 config.py 注释。
SEEDER_DEFAULTS = {
    "enabled": False,
    "interval_minutes": 240,
    "probability": 0.5,
    "daily_max": 2,
    "blocked_names": [],
}


def seeder_config(**overrides) -> types.SimpleNamespace:
    """世界事件播种配置夹具（默认与 config.py 出厂值一致，enabled=False 零行为）。"""
    merged = dict(SEEDER_DEFAULTS)
    merged.update(overrides)
    return types.SimpleNamespace(**merged)


class KvStoreMixin:
    """假 store 的 JSON 化 kv 契约（可混入）。

    v0.2.0 批 3-C5：话题偏好累积开始消费 ``get_kv`` / ``set_kv`` /
    ``get_kv_with_prefix``（真实 store 早已具备），而各测试文件的假 store 只实现了
    ``get_kv_int`` / ``get_kv_str`` 一族 → 8 个既有用例集体红。

    与其在三处各补一遍，混入本类即可；内部 dict **懒初始化**，各文件的 ``__init__``
    无需改动（这里用 ``hasattr`` 惰性探测，属测试夹具的合理豁免）。
    """

    @property
    def kv(self) -> dict:
        if not hasattr(self, "_kv_payload"):
            self._kv_payload = {}
        return self._kv_payload

    def get_kv(self, key: str) -> "dict | None":
        return self.kv.get(key)

    def set_kv(self, key: str, value: dict) -> None:
        self.kv[key] = value

    def get_kv_with_prefix(self, prefix: str) -> dict:
        return {item_key: item for item_key, item in self.kv.items() if item_key.startswith(prefix)}


# ─── 共享替身层（深化 D / 体检候选 D：测试面共享 seam） ─────────────
# 此前 21 个文件各自定义 Store 替身（四种形状）、17 个 Logger 变体、25 个
# make_* 装配变体——「interface 即测试面」被逐文件重造。收敛目标 = 1：
# FakeStore 是全接口**超集**替身（one adapter serving all），不做参数化变体。
# 渐进收敛纪律（A+D 执行方案 §2.2）：断言与用例数不增不减；指针型测试不碰；
# 真实 NarrativeStore 的测试（test_streams 等）不换本替身。


class FakeStore:
    """全接口 store 替身：**单表多视图**（与真实 NarrativeStore 同构——str/int/JSON
    只是同一 kv 表上的序列化差异，不是三个存储面）。

    方法集 = 全部消费方（creation/proactive/learning/render/telemetry/hook）的
    并集；行为一律**内存直存**，不做任何过滤/截断（那是被测代码的职责）。
    ``kv`` / ``kv_str`` 属性是同一张表的两个别名（断言便利）；``chronicle`` /
    ``events`` / ``metrics`` 列表供断言直接读取。

    ⚠️ 不继承 :class:`KvStoreMixin`（那是多文件混入的旧契约，内部有独立 dict）；
    本类覆写同名方法保持调用兼容。
    """

    def __init__(self) -> None:
        self._data: dict = {}
        self.events: list = []
        self.chronicle: list = []
        self.metrics: list = []
        self._chronicle_done: dict = {}

    # ── 单表视图 ──
    @property
    def kv(self) -> dict:
        return self._data

    @property
    def kv_str(self) -> dict:
        return self._data

    def get_kv(self, key):
        return self._data.get(key)

    def set_kv(self, key, value):
        self._data[key] = value

    def get_kv_with_prefix(self, prefix):
        return {k: v for k, v in self._data.items() if k.startswith(prefix)}

    # ── str / int 视图 ──
    def get_kv_str(self, key, default=""):
        value = self._data.get(key)
        return default if value is None else str(value)

    def set_kv_str(self, key, value):
        self._data[key] = str(value)

    def get_kv_int(self, key, default=0):
        try:
            return int(self._data.get(key, default))
        except (TypeError, ValueError):
            return default

    def set_kv_int(self, key, value):
        self._data[key] = int(value)

    def delete_keys_with_prefix(self, prefix):
        hits = [k for k in self._data if k.startswith(prefix)]
        for k in hits:
            del self._data[k]
        return len(hits)

    # ── 事件队列（支线素材 / 群观察；只存不删，语义由被测代码驱动） ──
    def push_event(self, event):
        self.events.append(event)

    def append_event(self, scope, kind, text, ts=None, **kwargs):
        self.events.append({"scope": scope, "kind": kind, "text": text, "ts": ts, **kwargs})

    def list_events(self, scope, limit=20):
        return [event for event in self.events if event.get("scope") == scope][:limit]

    def clear_all_events(self):
        self.events.clear()

    # ── 编年史（append-only + 当日幂等标记） ──
    def append_chronicle(self, scope, kind, text, ts=None):
        self.chronicle.append({"scope": scope, "kind": kind, "text": text, "ts": ts})

    def recent_chronicle(self, scope, limit=3):
        return [row for row in self.chronicle if row.get("scope") == scope][:limit]

    def is_chronicle_done(self, scope, kind, date):
        return bool(self._chronicle_done.get(f"chronicle:{scope}:{kind}:{date}"))

    def mark_chronicle_done(self, scope, kind, date):
        self._chronicle_done[f"chronicle:{scope}:{kind}:{date}"] = 1

    # ── 指标采样（Telemetry 落盘口） ──
    def append_metric(self, name, value, user_id="", scope="", ts=None):
        self.metrics.append(
            {"name": name, "value": value, "user_id": user_id, "scope": scope, "ts": ts}
        )


class FakeLogger:
    """结构化静默 logger：info/warning/error 分列表收集（供断言），debug 静默。"""

    def __init__(self) -> None:
        self.infos: list = []
        self.warnings: list = []
        self.errors: list = []

    def _record(self, sink):
        def _log(message, *args):
            sink.append(str(message % args if args else message))
        return _log

    def info(self, message, *args):
        self._record(self.infos)(message, *args)

    def warning(self, message, *args):
        self._record(self.warnings)(message, *args)

    def error(self, message, *args):
        self._record(self.errors)(message, *args)

    def debug(self, *args, **kwargs):
        pass


def make_logger(capture: "list | None" = None) -> types.SimpleNamespace:
    """静默 logger；``capture`` 传列表则额外把 info/warning 文本汇入该列表。"""
    logger = FakeLogger()
    if capture is not None:
        original_info, original_warning = logger.info, logger.warning

        def info(message, *args):
            original_info(message, *args)
            capture.append(str(message % args if args else message))

        def warning(message, *args):
            original_warning(message, *args)
            capture.append(str(message % args if args else message))

        logger.info = info
        logger.warning = warning
    return logger


# ─── 机械等价夹具（测试精简轮 T2：≥2 文件逐字相同的定义上收） ─────────────
# 收敛纪律（测试精简执行方案 §3.2）：共享版与被替换的本地定义**同源**，
# 刺激面零变化；测试文件以别名绑定保持原名与调用点不变。


class WarnLogger:
    """warning 收集、其余静默的 logger（原 4 文件逐字相同的 ``_Logger``）。

    四方法全部 ``*a, **k`` 宽容签名（生产侧多传 kwargs 不会炸）；
    ``warnings`` 列表供断言告警文案。
    """

    def __init__(self):
        self.warnings: list = []

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        self.warnings.append(a[0] % a[1:] if len(a) > 1 else str(a[0]))

    def error(self, *a, **k):
        pass


class CounterTelemetry:
    """只实现 ``record_counter`` 的 telemetry 替身（promotion/proposal 族共用）。"""

    def __init__(self):
        self.kinds: list = []

    def record_counter(self, kind: str, value: float = 1) -> None:
        self.kinds.append(kind)


class FakeCreator:
    """创作客户端替身：``generate`` 吞 prompt 返固定文案（生活片段链路用）。"""

    async def generate(self, prompt):
        return "今天在厨房煮了粥。"


class ListLogHandler(logging.Handler):
    """stdlib logging 捕获器：``messages`` 列表供断言告警内容。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def table_columns(db_path: Path, table: str) -> set:
    """读 sqlite 表的列名集合（store 建表/列断言用）。"""
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        connection.close()
    return {str(row[1]) for row in rows}


def pending_events(engine):
    """取 engine 自我层的 pending_events 列表（生活片段链路断言用）。"""
    return engine._self_state["state"]["focus"]["pending_events"]


def make_engine(*, config=None, state=None, store=None, creator=None, telemetry=None):
    """真 NarrativeEngine + 共享替身的标准装配（各文件 ``__new__``+手挂的单一化）。

    - ``config`` 缺省给最小可用段（plugin/narrative/llm/identity/anchor）；
    - ``state`` 缺省给标准自我层形状；传入则原样使用（load/save 直连该 dict）；
    - ``store`` 缺省新建 :class:`FakeStore`；``creator`` 缺省 ``None``（创作链
      测试自行注入 FakeCreator）。
    Returns:
        ``(engine, store)``——store 一并返回供断言。
    """
    if config is None:
        config = types.SimpleNamespace(
            plugin=types.SimpleNamespace(enabled=True),
            narrative=types.SimpleNamespace(
                enabled=True,
                mode_user_ids=["10001"],
                fragment_pending_max=12,
                life_fragment_daily_max=16,
                life_fragment_interval_minutes=30,
                life_fragment_detail_enabled=False,
                highlight_probability=0.0,
                chronicle_enabled=True,
                sleep_time="",
                wake_time="",
                wake_fragment_enabled=False,
                sleep_pre_sleep_hint_minutes=25,
            ),
            llm=types.SimpleNamespace(show_prompt=False, temperature=0.7),
            identity=types.SimpleNamespace(world="", values=[], world_rules=[], guard_fragments=[]),
            anchor=types.SimpleNamespace(guard_keywords=[]),
        )
    engine_mod = load("services.state.engine")
    resolved_store = store if store is not None else FakeStore()
    engine = engine_mod.NarrativeEngine.__new__(engine_mod.NarrativeEngine)
    engine._plugin = types.SimpleNamespace(
        config=config,
        ctx=types.SimpleNamespace(logger=make_logger()),
        _store=resolved_store,
        **({"_telemetry": telemetry} if telemetry is not None else {}),
    )
    engine._store = resolved_store
    if creator is not None:
        engine._creator = creator
    engine._self_state = state if state is not None else {
        "state": {
            "mood": {"label": "平静", "energy": 0.6},
            "routine": {"phase": "白天", "sleep_state": "awake"},
            "focus": {"pending_events": []},
        }
    }
    engine.load_self_state = lambda: engine._self_state
    engine.save_self_state = lambda value: None
    return engine, resolved_store


def run_standalone(globals_dict: dict) -> int:
    """独立运行入口：执行当前测试模块全部 test_ 函数并打印结果。

    供各测试文件 ``if __name__ == "__main__":`` 一行调用，保持
    pytest 之外的备用运行方式（返回码 0=全过，1=有失败）。
    """
    fns = [
        (name, obj)
        for name, obj in list(globals_dict.items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  [PASS] {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed} passed, {failed} failed")
    return 0 if not failed else 1
