"""MaiBot 剧本人设系统 — 插件入口。

v0.1 最小切片（设计树已闭合，.scratch/narrative-persona/）：
- 单人单私聊"剧本模式"（config.narrative.mode_user_ids 只填你自己）
- 双层状态机（自我层 + 支线层），结构化 + 编年史双写
- 世界时钟规则推进（零 LLM tick）+ 每日编年史压缩（轻量模型）
- 主动消息：活跃窗口 + 随机计时 + 静默时段 + 由头签发（禁止干聊）
- 表达学习隔离：剧本模式会话阻断表达注入与写入

构成：@HookHandler x5（入站落痕 / 出站采样 / 剧本注入 / 表达选择拦截 / 表达写入拦截）、
@Command + @API。接入点全部走命名 hook（chat.receive.after_process /
send_service.before_send / maisaka.planner.before_request / expression.*）；
本代架构消息经 heart_flow + 命名 hook，事件（ON_MESSAGE/POST_SEND）不再派发给插件。
不使用已废弃的 @Action（官方建议）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import asyncio
import contextlib
import datetime
import json
import time

from maibot_sdk import (
    API,
    Command,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import MaiNarrativePluginConfig
from .services import (
    NarrativeEngine,
    ProactiveScheduler,
    StreamRegistry,
    Telemetry,
    build_context_block,
    build_injected_item,
    is_injected_item,
)
from .services.state.engine import INJECT_TEXT_CAP, local_now
from .services.state.continuity import (
    SLOW_FIELD_PATHS,
    PromotionEngine,
    current_relationship_stage,
)
from .services.learning.drift_style import describe_drift
from .services.learning.projection import (
    get_style_projection,
    is_self_write_in_progress,
    rebuild_projection,
    write_projection,
)
# 互动配对（批 4-C2 / R31）：**只建不消费**——落点照建，晋升通道先不接。
# ⚠️ 注意本 import 只出现在 plugin 层：晋升链路（continuity/proposal/evidence）
# 禁止 import 本模块，由 pytests/test_pairs.py 的 AST 断言守住。
from .services.learning.pairs import PairTracker, message_id_of
# 慢变晋升（批 4-C3/C4/C5）：提案提炼 + 冷启动 seed
from .services.learning.evidence import positive_signal_days
from .services.learning.proposal import ProposalRunner, seed_perspective
from .services.proactive.scheduler import validate_rules
from .services.render.audience import drop_diary, filter_entries, visible_chronicle
from .services.render.replyer_block import build_replyer_block, build_style_item, is_style_item
from .services.message import (
    extract_user_id,
    is_private_chat,
    looks_like_command,
    message_text,
    outbound_text_len,
)
from .services.store import SOURCE_DIARY, NarrativeStore

# status 中可用列表的展示上限（超出截断，避免刷屏）
_AVAILABLE_SHOW_LIMIT = 10

#: 漂移注入耗时预算（ms）。宿主 hook 硬超时 6 秒，此处是**内部告警门槛**（R-C）：
#: 超预算说明注入路径被拖慢，宿主不会报错（异常全吞），只能靠这条 WARN 预警。
_DRIFT_BUDGET_MS = 500


def format_available_tasks_line(names: Optional[List[str]]) -> str:
    """把宿主 ``llm.get_available_models()`` 的结果格式化为 status 展示行；空则空串。

    ⚠ 宿主该能力返回的是**任务名**（utils/planner/replyer/…）而非注册模型名：
    ``plugin_runtime/capabilities/core.py`` → ``services/service_task_resolver.py``
    返回的是 ``model_task_config`` 的 TaskConfig 键。原先标成"可用模型"，用户会照抄
    任务名去填 ``[llm].creation_model``；故如实标注为「可用任务」并指引去 WebUI 查模型名。
    """
    items = [str(item).strip() for item in (names or [])]
    items = [item for item in items if item]
    if not items:
        return ""
    shown = ", ".join(items[:_AVAILABLE_SHOW_LIMIT])
    if len(items) > _AVAILABLE_SHOW_LIMIT:
        shown += "…"
    return f"可用任务（非模型名）: {shown}｜creation_model 请填 WebUI「模型列表」中的模型名"


def _sleep_status_line(cfg: Any, routine: Dict[str, Any]) -> str:
    """把自我层 routine 渲染成 status 的睡眠行（v0.1.10）。"""
    sleep_text = str(cfg.sleep_time or "").strip()
    wake_text = str(cfg.wake_time or "").strip()
    if not sleep_text or not wake_text:
        return "未配置（[narrative].sleep_time 留空，bot 全天不睡）"
    asleep = str(routine.get("sleep_state", "awake")) == "asleep"
    state_text = "睡眠中" if asleep else "清醒"
    woken = int(routine.get("woken_count", 0) or 0)
    if asleep and woken:
        state_text += f"（今晚被吵醒 {woken} 次）"
    delayed = str(routine.get("sleep_delayed_ts", "") or "")
    if not asleep and delayed:
        state_text += "（入睡推迟中）"
    return f"{state_text} | 作息 {sleep_text} - {wake_text}"


class MaiNarrativePlugin(MaiBotPlugin):
    """剧本人设系统主插件。"""

    config_model: type[PluginConfigBase] = MaiNarrativePluginConfig

    #: 漂移注入计数（批 3-E8）：宿主 hook 异常全被吞 → 必须自证「注入有没有上线」，
    #: 同「部署后必查 delivered/sent」的同款理由。声明为**类级默认值**：
    #: 单测常用 ``__new__`` 绕过 ``__init__``，类属性可读，避免假实例 AttributeError。
    _style_inject_count: int = 0

    #: 互动配对追踪（批 4-C2 / R31）。同为**类级**默认值：单测常用 ``__new__``
    #: 绕过 ``__init__``，而出站 hook 会读它（批 3 的 ``_style_inject_count`` 同款坑）。
    _pairs: Optional[PairTracker] = None

    #: 冷启动 seed 是否已在本进程尝试过（批 4-C5）。类级默认值同 ``_pairs`` 理由。
    _seed_attempted: bool = False

    #: 关系晋升节流间隔（分钟，批 4-C4）。读 metrics csv 不便宜，不能每 15s 跑一遍。
    _RELATION_PROMOTE_INTERVAL_MINUTES: int = 60

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[NarrativeStore] = None
        self._engine: Optional[NarrativeEngine] = None
        self._proactive: Optional[ProactiveScheduler] = None
        self._telemetry: Optional[Telemetry] = None
        # uid↔stream 注册表（含 kv 持久化，防重启后主动消息失联）
        self._streams: Optional[StreamRegistry] = None
        # 互动配对追踪（批 4-C2 / R31：只建不消费，为批 5 的 LLM 提案攒数据）
        self._pairs: Optional[PairTracker] = None
        # 慢变晋升机（批 4）：提案提炼器 + 晋升状态机
        self._promotion: Optional[ProposalRunner] = None
        self._promotion_engine: Optional[PromotionEngine] = None
        # 关系晋升节流（内存；重启即清 → 重启后立刻评估一次）
        self._last_relation_promote_ts: Optional[datetime.datetime] = None
        # 看门狗任务：不依赖 on_config_update 回调，主动对齐"配置开关 ↔ 后台任务"
        self._watchdog: Optional[asyncio.Task] = None

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("mai-narrative 已禁用（plugin.enabled=false），仅保留命令")
        self._warn_deprecated_config_keys()
        self._warn_window_rule_problems()
        data_dir = self.ctx.paths.data_dir / "narrative"
        self._store = NarrativeStore(data_dir)
        self._telemetry = Telemetry(self)
        self._engine = NarrativeEngine(self)
        self._proactive = ProactiveScheduler(self)
        # uid↔stream 注册表：启动时从 kv 回填（防重启后主动消息失联）
        self._streams = StreamRegistry(self._store, self.ctx.logger)
        self._streams.restore()
        # 互动配对追踪（批 4-C2 / R31）：pending 槽在内存，配对落 interaction_pairs 表。
        # **只建不消费**——批 4 的任何晋升判定都不读它（AST 断言见 test_pairs.py）。
        self._pairs = PairTracker(self._store, logger=self.ctx.logger)
        # 慢变晋升机（批 4-C3/C4）：提案提炼器 + 确定性晋升状态机。
        # ⚠️ 不在这里 seed——seed 要调 LLM（最多 30s），会拖慢插件加载；
        # 放到看门狗首轮（见 _maybe_seed_perspective）。
        self._promotion = ProposalRunner(self)
        self._promotion_engine = PromotionEngine(self)
        await self._reconcile_all()
        # R14：启动期锚定一致性比对（默认关，见 [anchor].consistency_check）。
        # 放在 _reconcile_all 之后、watchdog 之前——即便它慢/失败，后台任务已就绪。
        await self._check_anchor_consistency()
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="narrative-watchdog")
        self.ctx.logger.info(
            "mai-narrative v%s 已加载（剧本=%s 主动=%s 模式用户=%s 数据目录=%s）",
            self._plugin_version(),
            self.config.narrative.enabled,
            self.config.proactive.enabled,
            ",".join(self._mode_user_ids()) or "无",
            data_dir,
        )

    def _warn_deprecated_config_keys(self) -> None:
        """探测已废弃的旧配置键并告警（SDK extra="ignore" 会静默丢弃，用户无感知）。

        背景（2026-09-13）：``[llm].creation_task`` 已被 ``[llm].creation_model``
        （按模型名路由，依赖 MaiBot 1.2.5 #2031）取代；旧键残留在 config.toml
        时模型路由静默失效。经 SDK 公开接口 ``get_plugin_config_data()`` 读
        合并后的原始配置（未删的未知键保留其中）。
        """
        try:
            raw_config = self.get_plugin_config_data()
        except Exception as exc:
            self.ctx.logger.debug("读取原始配置数据失败（跳过弃用键检查）: %s", exc)
            return
        llm_raw = raw_config.get("llm")
        if isinstance(llm_raw, dict) and llm_raw.get("creation_task"):
            self.ctx.logger.warning(
                "检测到已废弃配置键 [llm].creation_task=%r（已不再生效，模型路由将走默认模型）。"
                "请改用 [llm].creation_model（填已注册模型名，详见 README「创作模型路由」）。",
                llm_raw["creation_task"],
            )
        proactive_raw = raw_config.get("proactive")
        if isinstance(proactive_raw, dict) and proactive_raw.get("user_active_windows"):
            self.ctx.logger.warning(
                "检测到已废弃配置键 [proactive].user_active_windows（dict 形态在 WebUI 显示为 "
                "[object Object] 且无法编辑，已不再生效，会退化为默认窗口）。"
                "请改用 [proactive].user_window_rules（数组，字段 user_id/days/start/end）。"
            )
        # 用键存在性而非真值判断：这两个字段的合法取值包含 0
        narrative_raw = raw_config.get("narrative")
        if isinstance(narrative_raw, dict):
            stale = [
                key
                for key in ("event_max_daily", "event_max_per_user_daily")
                if key in narrative_raw
            ]
            if stale:
                self.ctx.logger.warning(
                    "检测到已移除的配置键 %s（代码从未读取过，事件队列靠 3 天过期兜底，"
                    "并非靠它们限流）。残留值不再生效，可从 config.toml 删除。",
                    "、".join(f"[narrative].{key}" for key in stale),
                )

    def _warn_window_rule_problems(self) -> None:
        """启动期校验按用户窗口规则：非法条目运行期会被静默跳过，必须提前点名。

        与 ``_warn_deprecated_config_keys`` 同源纪律（参考 ``[llm].creation_task``
        教训）：静默失效是最贵的失败模式——用户以为配了免打扰，实际一条都没生效。
        """
        for problem in validate_rules(self.config.proactive.user_window_rules):
            self.ctx.logger.warning(
                "[proactive].user_window_rules 配置有误 → %s（该条运行期将被跳过）", problem
            )

    async def _check_anchor_consistency(self) -> None:
        """R14：启动期「宿主 [personality] vs 插件 world/values」LLM 一次性一致性比对。

        真机出现过「大二女大学生 vs 隐姓埋名神兽」双人格混写产物——锚定层分裂时
        模型两边都信。此处**只在配置开启时**跑一次；不一致仅 WARN 不阻断。

        ⚠️ 默认关（``[anchor].consistency_check=false``）：启动期同步 LLM 调用一旦
        慢或失败会拖垮启动。默认关 = 通道建好、默认不跑。任何异常只降级为 debug，
        绝不让它影响插件加载。
        """
        if not self.config.anchor.consistency_check:
            return
        identity = self.config.identity
        # 世界/价值观为空时无从比对（这是"没配世界观"，不是"不一致"）
        plugin_anchor = {
            "world": str(identity.world or "").strip(),
            "values": [str(item).strip() for item in (identity.values or []) if str(item).strip()],
        }
        if not plugin_anchor["world"] and not plugin_anchor["values"]:
            self.ctx.logger.debug("锚定一致性比对跳过：插件侧 world/values 均为空")
            return
        try:
            host_personality = str(
                await self.ctx.config.get("personality.personality", "") or ""
            ).strip()
        except Exception as exc:  # noqa: BLE001 — 读宿主配置失败不该拖垮启动
            self.ctx.logger.debug("读取宿主 personality 失败（跳过一致性比对）: %s", exc)
            return
        if not host_personality:
            self.ctx.logger.debug("锚定一致性比对跳过：宿主 personality 为空")
            return
        verdict = await self._judge_anchor_consistency(host_personality, plugin_anchor)
        if verdict is None:
            return
        conflicts = str(verdict.get("conflicts") or "").strip()
        if conflicts:
            self.ctx.logger.warning(
                "⚠️ 锚定层可能存在双人格冲突（宿主 personality vs 插件 world/values）：%s。"
                "建议核对 [identity].world/values 与宿主 [personality] 是否描述同一个人。",
                conflicts,
            )
        else:
            self.ctx.logger.info("锚定一致性比对通过：宿主人格与插件世界/价值观无冲突")

    async def _judge_anchor_consistency(
        self, host_personality: str, plugin_anchor: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """调 LLM 判定锚定层是否自相矛盾；失败返回 None（只降级，不阻断）。"""
        try:
            from .services.creation.creator import CreatorClient

            prompt = (
                "下面是同一个 AI 角色的两份设定。判断它们是否描述了**同一个角色**"
                "（身份/背景/年龄感等是否自相矛盾）。\n\n"
                f"【宿主人格】\n{host_personality[:600]}\n\n"
                f"【插件世界观】\n{plugin_anchor['world'][:300]}\n"
                f"【插件价值观】\n{'、'.join(plugin_anchor['values'][:5])}\n\n"
                "只回一行：若一致回 `一致`；若矛盾回 `冲突：<一句话说明>`。"
            )
            client = CreatorClient(self)
            text = await client.generate(prompt)
        except Exception as exc:  # noqa: BLE001 — 比对失败是可选增强，不阻断加载
            self.ctx.logger.debug("锚定一致性比对调用失败: %s", exc)
            return None
        normalized = str(text or "").strip()
        if not normalized:
            return None
        if normalized.startswith("冲突") or "矛盾" in normalized:
            return {"conflicts": normalized.split("：", 1)[-1].split(":", 1)[-1].strip() or normalized}
        return {"conflicts": ""}

    def _plugin_version(self) -> str:
        """从插件自带的 _manifest.json 读版本号（加载日志不再写死版本漂移文案）。"""
        try:
            manifest_path = Path(__file__).parent / "_manifest.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            version = str(data.get("version") or "").strip()
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("读取 _manifest.json 版本失败: %s", exc)
            return "未知"
        if not version:
            self.ctx.logger.warning("_manifest.json 缺少 version 字段")
            return "未知"
        return version

    async def on_unload(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        with contextlib.suppress(Exception):
            if self._engine is not None:
                await self._engine.stop()
        with contextlib.suppress(Exception):
            if self._proactive is not None:
                await self._proactive.stop()
        self.ctx.logger.info("mai-narrative 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载：按新开关重启后台任务。"""
        del config_data
        # 防重入（ADR-0002 决策 6）：插件自身写回 [learned] 时若又触发本回调，会形成
        # 「写回 → 回调 → 再写回」的环。批 4 起 tomlkit 会真的写回，此处先立闸。
        if is_self_write_in_progress():
            self.ctx.logger.debug("mai-narrative 忽略自身写回触发的配置更新: scope=%s", scope)
            return
        self.ctx.logger.info(
            "mai-narrative 配置更新: scope=%s version=%s（任务按新配置重排）",
            scope, version,
        )
        # 窗口规则改完立刻生效（调度器每 30s 重读），非法条目必须当场点名
        self._warn_window_rule_problems()
        await self._reconcile_all()

    async def _watchdog_loop(self) -> None:
        """看门狗：每 15s 对齐"配置开关 ↔ 引擎/主动任务"。

        背景（真机实测）：WebUI 打开剧本开关后，``on_config_update`` 若未触发，
        引擎不会自行启动（阶段/精力冻结在旧值）。看门狗让引擎在被启用后的
        15s 内自动拉起，不依赖回调时序。
        """
        try:
            while True:
                try:
                    await self._reconcile_all()
                    # 慢变晋升链路（批 4）：seed 只试一次；晋升自带间隔/退避闸门，
                    # 每 15s 调一次是廉价的（is_due 只读一个 kv 键）。
                    await self._maybe_seed_perspective()
                    await self._maybe_run_promotion()
                except Exception as exc:
                    self.ctx.logger.warning("narrative 看门狗异常: %s", exc, exc_info=True)
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            pass

    async def _reconcile_all(self) -> None:
        """按当前配置对齐引擎/主动调度器启停（幂等，on_load/on_config_update/看门狗共用）。"""
        if self._engine is None or self._proactive is None:
            return
        await self._engine.reconcile()
        await self._proactive.reconcile()

    # ===== 慢变晋升（批 4-C3/C4/C5）=====

    async def _maybe_seed_perspective(self) -> None:
        """冷启动播种（R15）：每进程只试一次；失败留空 + WARN，下次启动再试。

        ⚠️ 刻意**不在 on_load 里**做：seed 要调 LLM（超时 30s），放在加载期会拖慢
        插件加载；放看门狗首轮则启动即刻返回，播种在后台完成。
        """
        if self._seed_attempted:
            return
        if self._store is None or self._engine is None:
            return
        if not (self.config.plugin.enabled and self.config.narrative.enabled):
            # 开关没打开**不消耗**「已尝试」名额——用户随后打开开关仍能播种
            return
        self._seed_attempted = True
        try:
            result = await seed_perspective(self, now=self._local_now())
        except Exception as exc:  # 播种失败绝不阻断任何东西
            self.ctx.logger.warning("narrative 冷启动 seed 异常（不阻断）: %s", exc)
            return
        status = str(result.get("status") or "")
        if status == "ok":
            self.ctx.logger.info(
                "narrative 冷启动 seed 完成：world_view=%s", result.get("world_view")
            )
            self._project_learned()
        elif status not in ("already", "disabled"):
            self.ctx.logger.warning(
                "narrative 冷启动 seed 未完成（status=%s），下次启动会重试", status
            )

    async def _maybe_run_promotion(self) -> None:
        """慢变晋升 tick：提案 → 反证 → 晋升 → 关系确定性晋升。

        节流交给 ``ProposalRunner`` 自己的间隔/退避闸门（``is_due`` 只读一个 kv 键），
        所以 15s 调一次是廉价的。
        """
        if self._promotion is None or self._promotion_engine is None or self._store is None:
            return
        if not (self.config.plugin.enabled and self.config.narrative.enabled):
            return
        if not self.config.promotion.enabled:
            return
        now = self._local_now()

        result = await self._promotion.run(now=now)
        if str(result.get("status") or "") == "ok":
            # 反证先于晋升：被引用的旧提案先被驳回，免得它同一轮又被应用一次
            self._promotion_engine.apply_refutations(now=now)
            applied_any = False
            for row in self._store.list_proposals(status="pending", limit=200):
                if self._promotion_engine.apply(row, now=now).get("applied"):
                    applied_any = True
            if applied_any:
                # 有晋升落库才写回投影（E5：只投影 general 维度；relationship 绝不落盘）
                self._project_learned()

        self._maybe_promote_relationship(now)

    def _project_learned(self) -> None:
        """把 general 慢变现值写回 ``config.toml`` 的 ``[learned]`` 投影（批 4-C6）。

        ⚠️ 写回失败**不影响晋升**：db 才是事实源，投影损坏可从 db 重建
        （``rebuild_projection``）。此处只 WARN，绝不冒泡打断看门狗。
        """
        if self._engine is None:
            return
        try:
            written = write_projection(
                self._config_path(),
                self._engine.load_self_state(),
                logger=self.ctx.logger,
            )
        except Exception as exc:  # noqa: BLE001 —— 投影是附属品，不得反噬主链路
            self.ctx.logger.warning(
                "narrative [learned] 投影写回失败（不影响晋升，可从 db 重建）: %s", exc
            )
            return
        if written:
            self.ctx.logger.debug("narrative [learned] 投影已写回：%s", sorted(written))

    def _rebuild_learned(self) -> None:
        """权威重建 ``[learned]`` 投影（回滚后用：被清空的维度要一起从投影里消失）。

        与 :meth:`_project_learned` 的差别＝**会删**。回滚把某个 general 维度清空时，
        增量写回不会移除投影里的旧值，必须走重建。
        """
        if self._engine is None:
            return
        try:
            rebuild_projection(
                self._config_path(),
                self._engine.load_self_state(),
                logger=self.ctx.logger,
            )
        except Exception as exc:  # noqa: BLE001 —— 同 _project_learned：投影不得反噬主链路
            self.ctx.logger.warning(
                "narrative [learned] 投影重建失败（不影响回滚）: %s", exc
            )

    def _maybe_promote_relationship(self, now: datetime.datetime) -> None:
        """关系确定性晋升（强约束 1/2）：按正向场景日推进 trust / closeness。

        ⚠️ 节流 60 分钟：读 ``metrics/*.csv`` 不便宜，而关系值的变化尺度是天/周。
        ⚠️ 正向信号为 0 时 WARN 一次——最常见成因是 ``[telemetry].enabled`` 被关掉
        （关掉验收采样 = 关掉关系晋升的证据源，见 config.toml 的 [promotion] 注释）。
        """
        if self._promotion_engine is None or self._store is None:
            return
        if self._last_relation_promote_ts is not None:
            elapsed = (now - self._last_relation_promote_ts).total_seconds() / 60
            if elapsed < self._RELATION_PROMOTE_INTERVAL_MINUTES:
                return
        self._last_relation_promote_ts = now
        warned = False
        for uid in self._mode_user_ids():
            days = positive_signal_days(self._store, uid)
            if not days:
                if not warned:
                    self.ctx.logger.warning(
                        "叙事关系晋升：正向信号为 0（uid=%s）——请确认 [telemetry].enabled "
                        "与 [plugin].enabled 均为 true（关掉验收采样 = 关掉关系晋升的证据源）",
                        uid,
                    )
                    warned = True
                continue
            self._promotion_engine.promote_relationship(uid, days, now=now)

    # ===== 消息辅助（字段细节与 always-reply-private 一致） =====

    def _mode_user_ids(self) -> List[str]:
        """剧本模式用户 ID 列表（规范化字符串）。"""
        return [str(item) for item in (self.config.narrative.mode_user_ids or []) if str(item).strip()]

    def _is_mode_uid(self, user_id: str) -> bool:
        """判断用户是否是剧本模式用户。"""
        return bool(user_id) and user_id in set(self._mode_user_ids())

    def _is_mode_session(self, session_id: str) -> bool:
        """判断会话是否为剧本模式会话（显式白名单或已学习映射）。"""
        if not session_id:
            return False
        if session_id in (str(item) for item in (self.config.narrative.mode_stream_ids or [])):
            return True
        uid = self._streams.uid_of(session_id) if self._streams is not None else ""
        return self._is_mode_uid(uid)

    def _local_now(self) -> datetime.datetime:
        """按插件配置时区取本地时间（与引擎统一）。"""
        return local_now(self.config.narrative.timezone_offset_hours)

    def _config_path(self) -> Path:
        """插件自身 ``config.toml`` 路径（``[learned]`` 区块 tomlkit 直读写用）。

        ⚠️ 该区块**不在** ``config.py``，SDK 不解析、WebUI 不渲染（ADR-0002 决策 6），
        因此无法从 ``self.config`` 取，只能按文件位置定位。
        """
        return Path(__file__).resolve().parent / "config.toml"

    # ===== 入站 Hook：落痕 + 采样 + 支线反馈 =====
    # 接入点用 chat.receive.after_process（与 always-reply-private 同源，真机已验证送达）；
    # 本代架构消息走 heart_flow + 命名 hook，ON_MESSAGE 事件不再派发给插件。

    @HookHandler(
        "chat.receive.after_process",
        name="narrative_inbound",
        description="剧本模式私聊入站落痕与验收采样",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_inbound_message(self, **kwargs: Any) -> Dict[str, Any]:
        """用户消息到达：登记会话、更新互动状态、采集指标（Hook 契约返回 dict）。

        过滤分支均留 debug 级结构化日志（测试期排查入站链不生效用的 INFO
        已在 v0.1.4 部署稳定化时降级——每条消息都打会刷屏，但排查时仍在）。
        """
        message = kwargs.get("message")
        stream_id = str(
            kwargs.get("stream_id")
            or kwargs.get("session_id")
            or (message.get("session_id") if isinstance(message, dict) else "")
            or ""
        )
        if self._engine is None or self._store is None or self._telemetry is None:
            self.ctx.logger.warning("narrative inbound: 服务未初始化，跳过")
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(message, dict) or not message:
            self.ctx.logger.debug("narrative inbound: message 为空/非 dict（type=%s）", type(message).__name__)
            return {"action": "continue", "modified_kwargs": kwargs}

        user_id = extract_user_id(message)
        is_private = is_private_chat(message)
        is_mode = self._is_mode_uid(user_id)
        self.ctx.logger.debug(
            "narrative inbound: keys=%s | user_id=%r | stream_id=%r | is_private=%s | is_mode=%s | text=%s",
            sorted(message.keys()), user_id, stream_id, is_private, is_mode,
            message_text(message)[:30],
        )
        if not is_private:
            return {"action": "continue", "modified_kwargs": kwargs}
        if not is_mode:
            self.ctx.logger.debug("narrative inbound: 用户不在模式名单，uid=%r", user_id)
            return {"action": "continue", "modified_kwargs": kwargs}

        if stream_id:
            self._streams.record(user_id, stream_id)

        plain = message_text(message)
        # 命令/通知类消息不进剧本素材（命令是"你本人操作"，不是 bot 的生活）。
        # RESERVED(R9)：宿主 is_command 字段不可靠，补本地正则兜底——命令被当成
        # 对话素材会污染创作层与关系值。宿主修好后删掉 looks_like_command 即可。
        is_command = bool(message.get("is_command")) or looks_like_command(plain)
        if is_command or bool(message.get("is_notify")):
            self.ctx.logger.debug("narrative inbound: 命令/通知消息（is_command=%s is_notify=%s），跳过素材采集 uid=%s",
                                 message.get("is_command"), message.get("is_notify"), user_id)
            return {"action": "continue", "modified_kwargs": kwargs}

        now = self._local_now()
        self._engine.record_interaction(user_id, plain, now)
        self._engine.record_branch_feedback(user_id, now)
        # 验收采样（指标 1/2 的判定与登记下沉 Telemetry，2026-09-13 C5）
        self._telemetry.note_inbound(
            stream_id=stream_id, user_id=user_id, text=plain, now=now
        )
        # 验收指标 3：主动消息是否被接住（单指标 + 延迟分钟，2026-09-22 定案）
        #
        # 原先是 check_reply(30min) → elif check_late_reply(24h) 的判定链：一条用户
        # 消息只会落进其中一个分支，同时满足时 24h 的分子被吞掉，指标系统性低估
        # （A2 报告里的 24% 就是这么来的，真实值 78%）。改为单一入口 + 延迟连续量：
        # 有没有被接住用 count(*) 数，多快被接住用 avg(value) 看，口径在分析层切。
        latency = self._proactive.resolve_catch(user_id, now)
        if latency is not None:
            self._telemetry.record("proactive_replied", latency, user_id=user_id)
            # share_urge（v0.1.8）：被接住 → 正反馈（聊得起来，更想聊）
            self._engine.record_urge_feedback(user_id, "caught")
        elif self._telemetry.is_user_initiated(stream_id or user_id, now):
            # share_urge（v0.1.8）：用户主动发起（非回复主动消息）→ 被需要感
            self._engine.record_urge_feedback(user_id, "user_initiated")
        # 互动配对（批 4-C2 / R31）：把本次入站登记为「待配对的用户反馈」，
        # 等本轮出站时与之配成（用户反馈 id → 已送达回应 id）。只建不消费。
        if self._pairs is not None:
            self._pairs.note_inbound(user_id, message_id_of(message), now)
        self.ctx.logger.debug("narrative inbound: 落痕完成 uid=%s stream=%s", user_id, stream_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== 出站 Hook：采样（受入站事件不派发影响，出站同样改用命名 hook） =====

    @HookHandler(
        "send_service.after_build_message",
        name="narrative_post_send",
        description="剧本模式出站采样（对话深度/轮次/成本对照侧）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_post_send(self, **kwargs: Any) -> Dict[str, Any]:
        """bot 出站消息构建完成后：记录出站时刻/长度与对话轮次配对（指标 1/2 的对照侧）。

        挂载点说明（2026-09-08 修复）：曾挂在 ``send_service.before_send``，但其
        载荷没有 stream_id，轮次配对与 ``_last_bot_sent`` 结构上无法工作，且真机
        上 handler 疑似从未被派发（bot_msg_len 上线起 0 条）。``after_build_message``
        载荷含 stream_id，派发点位于发送链路外层 try/except 内，异常不再静默。
        """
        message = kwargs.get("message")
        resolved_stream = str(kwargs.get("stream_id") or kwargs.get("session_id") or "")
        if self._telemetry is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        # 触发层追踪：部署后临时调 debug 日志级别，一轮对话即可确认本 hook 是否被派发
        self.ctx.logger.debug("narrative outbound: stream=%s", resolved_stream or "-")
        uid = self._streams.uid_of(resolved_stream)
        # 主动开口送达确认（2026-09-22）：proactive_sent 记的是"触发"，而触发后
        # 模型可能选择沉默、也可能 reply 工具失败——只有真正出站了才算数，它才是
        # 承接率的真分母，也只有它才会因无人回应而罚冷落。
        if self._proactive is not None and self._proactive.mark_delivered(
            resolved_stream, self._local_now()
        ):
            self._telemetry.record("proactive_delivered", 1, user_id=uid, scope="proactive")
        # 出站采样（出站时刻/轮次配对/bot 长度判定下沉 Telemetry，2026-09-13 C5）
        self._telemetry.note_outbound(
            stream_id=resolved_stream,
            user_id=uid,
            message=message,
            now=self._local_now(),
        )
        # 互动配对（批 4-C2 / R31）：把本轮出站与最近一条待配对入站配成一对并落盘。
        # 只建不消费；落盘失败静默（配对是旁路，绝不拖垮发送链路）。
        if self._pairs is not None:
            self._pairs.note_outbound(uid, message_id_of(message), self._local_now())
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== Hook：剧本上下文注入 =====

    @HookHandler(
        "maisaka.planner.before_request",
        name="narrative_inject_life_context",
        description="剧本模式会话在规划请求前注入剧本生活状态",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_life_context(self, **kwargs: Any) -> Dict[str, Any]:
        """把自我层/支线层/编年史渲染成 item 追加进请求。"""
        # A/B 对照 gate（2026-09-10）：narrative.enabled=false 时剧本行为全停，
        # 但入站/出站采样 hook 无本 gate 照常采集——对照组数据口径的关键。
        if not (self.config.plugin.enabled and self.config.narrative.enabled):
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id") or "")
        items = kwargs.get("items")
        if self._engine is None or self._store is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        if not self._is_mode_session(session_id):
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(items, list) or not items:
            return {"action": "continue", "modified_kwargs": kwargs}
        if any(is_injected_item(item) for item in items):
            return {"action": "continue", "modified_kwargs": kwargs}

        user_id = self._streams.uid_of(session_id)
        state = self._engine.load_self_state()
        branch = self._engine.load_branch_state(user_id) if user_id else None
        # 受众过滤（ADR-0004 第 2 层）：涉私素材只讲给本人，diary 产物对所有人短路
        recent = visible_chronicle(self._store, "self", user_id, 3)
        round_kind, bysource = self._proactive.consume_pending(session_id)
        context_text = build_context_block(
            self,
            state,
            branch,
            self._local_now(),
            recent,
            round_kind=round_kind,
            bysource=bysource,
            audience=user_id,
        )
        items.append(build_injected_item(context_text))
        kwargs["items"] = items
        # 注入追踪（含日照锚点/心情/精力段，debug 级不刷盘时需临时调高日志级别）
        self.ctx.logger.debug(
            "narrative 注入: stream=%s | %s",
            session_id or "-",
            context_text[:100].replace("\n", " "),
        )
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== Hook：漂移层调制注入（批 3-C3） =====

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="narrative_inject_drift_style",
        description="剧本模式会话在回复生成前注入漂移层调制（此刻状态/关系语境/文学授权）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_drift_style(self, **kwargs: Any) -> Dict[str, Any]:
        """把此刻状态调制 + 关系语境 + 文学授权追加进 replyer 请求 items。

        ⚠️ 三条硬约束（ADR-0003 §5 + 宿主源码实测 H2/H3/H7）：
        1. 宿主 ``modified_kwargs`` 是**整体替换非合并** → 必须全量带出 kwargs，只改 items；
        2. hook 有 **6 秒硬超时** → 本路径只读一次 state，不读编年史，零 LLM；
        3. 宿主 try/except **吞掉一切异常** → 注入失败完全静默，故埋耗时与注入计数。

        与 planner 注入块分工（E9）：本块只出调制/关系/授权，**不重复**生活内容。
        """
        # A/B 对照 gate：与 planner 注入同款双开关，narrative.enabled=false 时行为全停
        if not (self.config.plugin.enabled and self.config.narrative.enabled):
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id") or "")
        items = kwargs.get("items")
        if self._engine is None or self._store is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        # 会话过滤：``session_id`` 实测原样等于 stream_id（H8）→ 直接复用判定
        if not self._is_mode_session(session_id):
            return {"action": "continue", "modified_kwargs": kwargs}
        if not isinstance(items, list) or not items:
            return {"action": "continue", "modified_kwargs": kwargs}
        # 幂等：同轮若已有本块（多 handler / 重入）不再追加
        if any(is_style_item(item) for item in items):
            return {"action": "continue", "modified_kwargs": kwargs}

        started = time.perf_counter()
        user_id = self._streams.uid_of(session_id)
        state = self._engine.load_self_state()
        branch = self._engine.load_branch_state(user_id) if user_id else None
        relationship = (branch or {}).get("relationship") or {}
        context_text = build_replyer_block(
            drift_text=describe_drift(state.get("state") or {}),
            # 与 /narrative status 同款取值顺序：显式 ``stage``（批 4 晋升机写入的晋升值）
            # 优先，批 4 前回退到只读事实推导（``continuity.current_relationship_stage``）。
            stage=str(
                relationship.get("stage")
                or current_relationship_stage(relationship)
            ),
            # 私聊剧本模式下受众即归属人本人；群聊就绪时两者分离，可见性 fail-closed
            audience=user_id,
            owner=user_id,
            learned_style=get_style_projection(self._config_path(), logger=self.ctx.logger),
        )
        items.append(build_style_item(context_text))
        kwargs["items"] = items
        self._style_inject_count += 1

        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > _DRIFT_BUDGET_MS:
            self.ctx.logger.warning(
                "narrative 漂移注入耗时 %.0fms 超预算(%dms)：宿主硬超时 6s，超时即静默回退",
                elapsed_ms,
                _DRIFT_BUDGET_MS,
            )
        self.ctx.logger.debug(
            "narrative 漂移注入: stream=%s 耗时=%.0fms | %s",
            session_id or "-",
            elapsed_ms,
            context_text[:100].replace("\n", " "),
        )
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== Hook：表达学习隔离 =====

    @HookHandler(
        "expression.select.before_select",
        name="narrative_block_expression_select",
        description="剧本模式会话阻断表达学习注入（防稀释人设）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def block_expression_select(self, **kwargs: Any) -> Dict[str, Any]:
        """剧本模式会话直接 abort，让表达选择整体跳过。"""
        # 隔离是"剧本模式"专属行为：任一开关关闭时放行（continue，不是 abort）。
        # 2026-09-16 前无此判断，插件/剧本关闭期间仍会 abort 表达选择。
        cfg = self.config
        if not (cfg.plugin.enabled and cfg.narrative.enabled):
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id") or "")
        if self._is_mode_session(session_id):
            return {"action": "abort", "modified_kwargs": kwargs}
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "expression.learn.before_upsert",
        name="narrative_block_expression_upsert",
        description="剧本模式会话阻断表达学习写入（防污染 expressions 表）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def block_expression_upsert(self, **kwargs: Any) -> Dict[str, Any]:
        """剧本模式会话 abort 单条写入。"""
        # 同 block_expression_select：任一开关关闭即放行，交还给主程序处理
        cfg = self.config
        if not (cfg.plugin.enabled and cfg.narrative.enabled):
            return {"action": "continue", "modified_kwargs": kwargs}
        session_id = str(kwargs.get("session_id") or "")
        if self._is_mode_session(session_id):
            return {"action": "abort", "modified_kwargs": kwargs}
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== Command：/narrative =====

    @Command(
        "narrative",
        description="剧本人设系统管理命令：/narrative help 查看用法",
        pattern=r"^\s*/narrative(?:\s+(?P<sub>.+))?\s*$",
    )
    async def handle_narrative_command(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """管理命令：help / status / reset。"""
        matched = (kwargs.get("matched_groups") or {}).get("sub") or ""
        stream_id = str(kwargs.get("stream_id", "") or "")
        user_id = str(kwargs.get("user_id", "") or "")
        if not self._is_admin(user_id):
            admin_list = list(self.config.plugin.admin_qq or [])
            msg = "⚠️ 未配置管理员" if not admin_list else "⚠️ 仅管理员可用"
            await self.ctx.send.text(msg, stream_id)
            return False, "no admin", True

        raw = str(matched or "").strip()
        if not raw or raw == "help":
            await self._cmd_help(stream_id)
            return True, "ok", True
        command, _, param = raw.partition(" ")
        if command == "status":
            await self._cmd_status(stream_id, user_id)
            return True, "ok", True
        if command == "reset":
            await self._cmd_reset(param, stream_id)
            return True, "done", True
        if command == "rollback":
            await self._cmd_rollback(param, stream_id)
            return True, "done", True
        await self.ctx.send.text(f"未知子命令: {command}。/narrative help 查看用法", stream_id)
        return False, "unknown sub", True

    def _is_admin(self, user_id: str) -> bool:
        """管理员白名单校验。"""
        admin_list = [str(item) for item in (self.config.plugin.admin_qq or [])]
        if not admin_list:
            return False
        return user_id in set(admin_list)

    async def _cmd_help(self, stream_id: str) -> None:
        text = (
            "/narrative help            - 查看本帮助\n"
            "/narrative status          - 剧本状态摘要（模式/心情/关系/编年史）\n"
            "/narrative reset           - 重置状态与事件（先输入 'reset' 显示确认）\n"
            "/narrative rollback [路径] - 撤销最近一次慢变晋升（last=最近一条；\n"
            "                             路径如 perspective.world_view）\n"
            "说明：配置在 WebUI 插件页修改（[identity] 锚定层人设请手动填写）。"
        )
        await self.ctx.send.text(text, stream_id)

    async def _cmd_rollback(self, param: str, stream_id: str) -> None:
        """撤销最近一次慢变晋升（批 4-C8）。

        ``/narrative rollback`` 或 ``... last`` → 全表最近一条 applied；
        ``/narrative rollback <path>`` → 该路径最近一条。
        """
        if self._promotion_engine is None or self._store is None:
            await self.ctx.send.text("⚠️ 晋升机未启用（[promotion].enabled=false）", stream_id)
            return
        requested = str(param or "").strip()
        if requested in ("", "last"):
            path = ""
        elif requested in SLOW_FIELD_PATHS:
            path = requested
        else:
            valid = "、".join(sorted(SLOW_FIELD_PATHS))
            await self.ctx.send.text(
                f"⚠️ 路径不在慢变白名单：{requested}\n可选：{valid}（或 last）", stream_id
            )
            return

        result = self._promotion_engine.rollback_last(path=path, now=self._local_now())
        status = str(result.get("status") or "")
        if status == "nothing":
            await self.ctx.send.text(
                "没有可撤销的晋升记录"
                + (f"（路径 {path}）" if path else "")
                + "。",
                stream_id,
            )
            return
        if status == "missing_scope":
            await self.ctx.send.text(
                f"⚠️ 该晋升是关系维度（{result.get('path')}）但审计缺少归属用户，"
                "拒绝瞎猜——请手工核对后处理。",
                stream_id,
            )
            return

        target = str(result.get("path") or "")
        await self.ctx.send.text(
            f"↩️ 已撤销最近一次晋升：{target}\n"
            f"恢复旧值：{result.get('restored')!r}\n"
            f"（审计 promotion_id={result.get('promotion_id')}）",
            stream_id,
        )
        # 投影跟着事实源走（只 general 维度才需要动 config.toml）
        if target.startswith("perspective."):
            self._rebuild_learned()

    async def _cmd_status(self, stream_id: str, audience: str = "") -> None:
        """状态摘要（不含聊天正文）。

        status 是对外面（发给命令发起者），同样按受众过滤（D6）：
        涉私素材只显示给本人，diary 产物一律不显示。
        """
        if self._engine is None or self._store is None:
            await self.ctx.send.text("插件尚未初始化完成，请稍后再试", stream_id)
            return
        cfg = self.config
        state = self._engine.load_self_state()
        inner = state["state"]
        # R10 退役（v0.2.0 批 1）：[creator_model] 直连已删，只剩按名路由一条路
        if str(cfg.llm.creation_model or "").strip():
            creator_line = f"创作模型: 按名路由（{cfg.llm.creation_model}）"
        else:
            creator_line = "创作模型: 默认（未配置，可填 [llm].creation_model）"
        mode_uids = self._mode_user_ids()
        lines = [
            "【剧本人设系统 · 状态】",
            f"剧本模式: {'开' if cfg.narrative.enabled else '关'} | "
            f"主动消息: {'开' if cfg.proactive.enabled else '关'}",
            creator_line,
            # N2 脱敏（D10/Q5）：状态文本曾把 mode_user_ids 原文外发给非管理员会话，
            # 真实事故——2026-09-22 14:12 发给 927386371 的状态含他人 QQ 号。
            # 只显示计数，不显示号码。
            f"模式用户: {len(mode_uids)} 人 | 已知会话: {self._streams.known_count()}",
            f"心情: {inner['mood']['label']}（精力 {inner['mood']['energy'] * 10:.0f}/10）| "
            f"阶段: {inner['routine']['phase']}",
            f"睡眠: {_sleep_status_line(cfg.narrative, inner.get('routine', {}))}",
        ]
        # 生活片段（创作层产出，v0.1.3 起是"心里挂念"的唯一来源）
        visible_pending = filter_entries(
            inner.get("focus", {}).get("pending_events", []), audience
        )
        if visible_pending:
            latest = str(visible_pending[-1].get("text", "") or "").strip()
            if latest:
                lines.append(f"生活片段: {latest[:40]}")
        # 同上：支线行也用序号代号，不外发 QQ 号。批 2 起不再显示熟悉/信任数字
        # （旧规则线只进不退的假演化已废弃，E2 裁决）——只留 stage 标签。
        for index, uid in enumerate(mode_uids, start=1):
            branch = self._engine.load_branch_state(uid)
            relationship = branch.get("relationship", {})
            stage = str(
                relationship.get("stage")
                or current_relationship_stage(relationship)
            )
            lines.append(f"支线[{index}]: {stage}")
        recent = visible_chronicle(self._store, "self", audience, 2)
        if recent:
            lines.append("编年史最近: " + str(recent[0].get("text", ""))[:40])
        today = self._local_now().strftime("%Y-%m-%d")
        for index, uid in enumerate(mode_uids, start=1):
            count = self._store.get_kv_int(f"proactive:count:{uid}:{today}")
            lines.append(f"今日主动[{index}]: {count}")
        # 学习层投影（批 3-C2 / R1）：[learned] 不在 config.py，只能从文件直读
        learned_style = get_style_projection(self._config_path(), logger=self.ctx.logger)
        if learned_style:
            lines.append(f"学习投影(R1): {len(learned_style)} 条")
        else:
            lines.append("学习投影(R1): 空（待批 4 晋升机写入）")
        # 漂移注入计数（E8）：宿主 hook 异常全吞，只有这里能证明「注入到底有没有上线」
        lines.append(f"漂移注入: 累计 {self._style_inject_count} 次")
        lines.append(f"数据目录: {self.ctx.paths.data_dir / 'narrative'}")
        # 宿主只开放「任务名」列表（非模型名），仅作连通性参考；失败不影响 status
        try:
            available = await self.ctx.llm.get_available_models()
            line = format_available_tasks_line([str(item) for item in (available or [])])
            if line:
                lines.append(line)
        except Exception as exc:
            self.ctx.logger.debug("获取可用任务列表失败: %s", exc)
        await self.ctx.send.text("\n".join(lines), stream_id)

    async def _cmd_reset(self, param: str, stream_id: str) -> None:
        """重置剧本状态：需显式确认 'yes' 或 'y'。"""
        normalized = str(param or "").strip().lower()
        if normalized not in ("yes", "y"):
            await self.ctx.send.text(
                "⚠️ 重置会清空自我层/支线层状态、事件队列与今日计数。"
                "确认请执行：/narrative reset yes",
                stream_id,
            )
            return
        deleted = self._store.delete_keys_with_prefix("")
        # 事件队列一并清空；编年史 append-only 刻意保留
        self._store.clear_all_events()
        self._streams.clear()
        self._telemetry.clear_pending_rounds()
        self._proactive.clear_sent()
        # 全清 kv 会连 state 一起抹掉 → 立即重建默认 state（带当前 schema_version）。
        # 否则下次开库发现「没有 state」会当作首次初始化——语义上没错，但若日后
        # schema_version 改存 kv 键，就会被误判成「旧库」反复重置（批 2 F2 的坑）。
        self._engine.load_self_state()
        await self.ctx.send.text(
            f"已重置叙事状态（kv {deleted} 项、事件队列已清空；编年史保留未动）。", stream_id
        )

    # ===== API：状态查询（仅元信息，不含正文） =====

    @API(
        "narrative_state",
        description="查询剧本状态摘要（模式/心情/关系/编年史计数，不含聊天正文）。",
        version="1",
        public=False,
    )
    async def handle_narrative_state_api(self, **kwargs: Any) -> Dict[str, Any]:
        """供调试/外部读取的状态摘要。"""
        del kwargs
        if self._engine is None or self._store is None:
            return {"ok": False, "error": "not_initialized"}
        state = self._engine.load_self_state()
        summary: Dict[str, Any] = {
            "narrative_enabled": self.config.narrative.enabled,
            "proactive_enabled": self.config.proactive.enabled,
            "mood": state["state"]["mood"]["label"],
            "energy": state["state"]["mood"]["energy"],
            "routine_phase": state["state"]["routine"]["phase"],
            "mode_user_ids": self._mode_user_ids(),
            "known_streams": self._streams.known_count(),
            "chronicle_count": self._store.count_chronicle("self"),
        }
        summary["branches"] = {
            uid: {
                "stage": str(
                    self._engine.load_branch_state(uid)
                    .get("relationship", {})
                    .get("stage")
                    or "陌生人"
                ),
                # RESERVED(R6)：互动计数是**内部证据计数**，不进 API 输出
                # （ADR-0002 §2：不进注入块、不进 status）
            }
            for uid in self._mode_user_ids()
        }
        return {"ok": True, **summary}

    # ===== API：日记插件握手（跨插件协作，public=True） =====

    @API(
        "narrative_diary_context",
        description=(
            "供 mai-diary 等插件握手：返回剧本会话判定所需的用户/会话列表，"
            "以及自我层人格摘要（锚定 identity + 心情 + 作息 + 最近编年史）。"
            "不含聊天正文；编年史仅回 60 字截断片段。"
        ),
        version="1",
        public=True,
    )
    async def handle_narrative_diary_context_api(self, **kwargs: Any) -> Dict[str, Any]:
        """日记生成时的剧本模式分诊数据源。"""
        del kwargs
        if self._engine is None or self._store is None:
            return {"ok": False, "available": False, "error": "not_initialized"}

        cfg = self.config
        narrative_on = bool(cfg.plugin.enabled and cfg.narrative.enabled)
        state = self._engine.load_self_state()
        inner = state["state"]
        identity = cfg.identity

        # 锚定层人设 → 作者人格描述：复用原生 [personality].personality（人设唯一来源）+
        # 插件世界观字段。与 render.build_context_block 同源原则：本 API 只回摘要。
        # 表达风格提示 expression_hint 同样从原生 reply_style 派生（人设唯一事实源，
        # 插件不再重复定义性格/语气——曾用 immutable_traits，与原生冲突已删除）。
        persona_parts: List[str] = []
        try:
            native_personality = str(
                await self.ctx.config.get("personality.personality", "") or ""
            ).strip()
        except Exception as exc:
            native_personality = ""
            self.ctx.logger.debug("读取原生 personality 失败: %s", exc)
        if native_personality:
            persona_parts.append(native_personality)
        if identity.world:
            persona_parts.append(f"生活在{identity.world}")
        try:
            native_reply_style = str(
                await self.ctx.config.get("personality.reply_style", "") or ""
            ).strip()
        except Exception as exc:
            native_reply_style = ""
            self.ctx.logger.debug("读取原生 reply_style 失败: %s", exc)
        expression_hint = native_reply_style

        mood = inner["mood"]
        return {
            "ok": True,
            "available": True,
            "narrative_enabled": narrative_on,
            "mode_user_ids": self._mode_user_ids(),
            "mode_stream_ids": [str(item) for item in (cfg.narrative.mode_stream_ids or [])],
            "self_state": {
                "identity_persona": "，".join(persona_parts),
                "expression_hint": expression_hint,
                "mood_label": str(mood.get("label", "平静")),
                "mood_energy": float(mood.get("energy", 0.5)),
                "mood_shift_ts": str(mood.get("last_shift_ts", "")),
                "routine_phase": str(inner["routine"].get("phase", "")),
                # 2026-09-26 用户裁定删除 hot_thread（推翻同日批 2 的「冻结保留」决定）：
                # 该字段自 v0.1.3 起无写入点（恒空），diary 侧读取点已同轮删除
                # （`glcoge-mai-diary/services/diary/prompts.py`）→ 双侧 lockstep 收敛，
                # 不存在单侧"静默降级"缺口。细节见登记表「跨插件契约字段」节。
                "latest_life_fragment": (
                    str(
                        list(inner.get("focus", {}).get("pending_events", []))[-1]
                        .get("text", "")
                    )[:INJECT_TEXT_CAP]
                    if inner.get("focus", {}).get("pending_events")
                    else ""
                ),
                # RESERVED(R18)：口径维持现状（继续给原文，用户 Q1-b 裁定）。
                # 但「diary 产物完全隔离」是立项铁律，不随该裁定豁免 → 只短路 diary。
                "recent_chronicle": [
                    str(entry.get("text", ""))[:INJECT_TEXT_CAP]
                    for entry in drop_diary(self._store.recent_chronicle("self", limit=3))
                    if str(entry.get("text", "")).strip()
                ],
            },
            # TODO(Round 3)：每日心情轨迹表尚未建（v0.1 只有当前快照）。
            # 情绪轨迹增强排期靠后，日记侧已预留注入点，先返回空列表。
            "today_mood_track": [],
        }

    @API(
        "narrative_chronicle_append",
        description=(
            "幂等写入一条自我层编年史（scope=self，kind=diary）。"
            "同一天重复写入返回 written=False。供 mai-diary 04:00 钩子调用。"
            "可选 audience：该条素材的可见受众；不传则按 diary 产物标记（完全隔离）。"
        ),
        version="1",
        public=True,
    )
    async def handle_narrative_chronicle_append_api(
        self,
        date: str = "",
        content: str = "",
        audience: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """把当日日记成品写入编年史（append-only，幂等）。

        RESERVED(R13)：``audience`` 是 diary 侧未来打标推送的**协议位**。
        当前 diary 产物完全隔离（ADR-0004），故该值不影响可见性——
        ``kind="diary"`` 已在读取侧一律短路。落库语义按 D2：
        未打标（None/空）→ ``source_uid=SOURCE_DIARY``；打标 → 素材归属该受众。
        """
        del kwargs
        date = str(date or "").strip()
        content = str(content or "").strip()
        if not date or not content:
            return {"ok": False, "written": False, "error": "date/content 不能为空"}

        cfg = self.config
        if not (cfg.plugin.enabled and cfg.narrative.enabled and cfg.narrative.chronicle_enabled):
            return {"ok": True, "written": False, "date": date, "reason": "chronicle_disabled"}
        if self._store is None:
            return {"ok": False, "written": False, "error": "not_initialized"}

        normalized_audience = str(audience or "").strip()
        written = self._store.append_chronicle_once(
            "self",
            "diary",
            content,
            date,
            source_uid=normalized_audience or SOURCE_DIARY,
            audience=normalized_audience,
        )
        return {
            "ok": True,
            "written": written,
            "date": date,
            "reason": "" if written else "duplicate",
        }


def create_plugin() -> MaiNarrativePlugin:
    """工厂函数：Runner 通过此函数实例化插件。"""
    return MaiNarrativePlugin()