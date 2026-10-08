"""MaiBot 剧本人设系统 — 插件入口。

v0.1 最小切片（设计树已闭合，.scratch/narrative-persona/）：
- 单人单私聊"剧本模式"（config.narrative.mode_user_ids 只填你自己）
- 双层状态机（自我层 + 支线层），结构化 + 编年史双写
- 世界时钟规则推进（零 LLM tick）+ 每日编年史压缩（轻量模型）
- 主动消息：活跃窗口 + 随机计时 + 静默时段 + 由头签发（禁止干聊）
- 表达学习隔离：剧本模式会话阻断表达注入与写入

构成：@HookHandler x6（入站落痕 / 出站采样 / 剧本注入 / 漂移注入 / 表达选择拦截 / 表达写入拦截）、
@ReplyExtension（主动轮由头进 replyer 指令位，v0.3.0 批 4）、@Command + @API。
接入点全部走命名 hook（chat.receive.after_process /
send_service.after_build_message / maisaka.planner.before_request /
maisaka.replyer.before_model_request / expression.*）；
本代架构消息经 heart_flow + 命名 hook，事件（ON_MESSAGE/POST_SEND）不再派发给插件。
不使用已废弃的 @Action（官方建议）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import asyncio
import contextlib
import datetime
import json

from maibot_sdk import (
    API,
    Command,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
    ReplyExtension,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import MaiNarrativePluginConfig
from .services import (
    inbound,
    inject,
    apis,
    commands,
    outbound,
    GroupStreamRegistry,
    LorebookLoader,
    NarrativeEngine,
    ProactiveScheduler,
    StreamRegistry,
    Telemetry,
)
from .services.state.engine import INJECT_TEXT_CAP, local_now
from .services.state.continuity import (
    SLOW_FIELD_PATHS,
    PromotionEngine,
    current_relationship_stage,
)
from .services.learning.projection import (
    get_style_projection,
    is_self_write_in_progress,
    rebuild_projection,
    write_projection,
)
# 互动配对（批 4-C2 / R31）：**只建不消费**——落点照建，晋升通道先不接。
# ⚠️ 注意本 import 只出现在 plugin 层：晋升链路（continuity/proposal/evidence）
# 禁止 import 本模块，由 pytests/test_pairs.py 的 AST 断言守住。
from .services.learning.pairs import PairTracker
# 慢变晋升（批 4-C3/C4/C5）：提案提炼 + 冷启动 seed
from .services.learning.evidence import positive_signal_days
from .services.learning.proposal import ProposalRunner, seed_perspective
from .services.proactive.scheduler import validate_rules
from .services.render.audience import drop_diary, filter_entries, visible_chronicle
from .services.inject import _REPLY_EXTENSION_NAME
from .services.kvkeys import PROACTIVE_COUNT as _PROACTIVE_COUNT_KEY
from .services.store import SOURCE_DIARY, NarrativeStore










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
        # gid↔session 注册表（R35：replyer hook 载荷没有群字段，只能靠入站登记）
        self._group_streams: Optional[GroupStreamRegistry] = None
        # R35 侦察信号：进程内首条群消息打一条 INFO（见 ``_observe_group``）
        self._group_recon_logged: bool = False
        # 互动配对追踪（批 4-C2 / R31：只建不消费，为批 5 的 LLM 提案攒数据）
        self._pairs: Optional[PairTracker] = None
        # 世界书 loader（v0.3.0 批 1 / R43：on_load 按 [lorebook].enabled 创建）
        self._lorebook: Optional[LorebookLoader] = None
        # REPLY_EXTENSION 组件全名（批 4 / R39：on_load 按 manifest id 组装；
        # scheduler 的 reason 教学与 handler 都经它取用，缺省空串=不教学）
        self.reply_extension_full_name: str = ""
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
        # R35：群会话映射同样要回填（不回填 = 重启后群漂移层注入静默停摆）
        self._group_streams = GroupStreamRegistry(self._store, self.ctx.logger)
        self._group_streams.restore()
        # 互动配对追踪（批 4-C2 / R31）：pending 槽在内存，配对落 interaction_pairs 表。
        # **只建不消费**——批 4 的任何晋升判定都不读它（AST 断言见 test_pairs.py）。
        self._pairs = PairTracker(self._store, logger=self.ctx.logger)
        # 世界书 loader（v0.3.0 批 1 / R43）：enabled=false（默认）不创建——
        # 零行为双保险的一半（另一半在 planner_block 的 detailed 模式闸）。
        self._lorebook = None
        if self.config.plugin.enabled and self.config.lorebook.enabled:
            self._lorebook = LorebookLoader(
                data_dir / str(self.config.lorebook.file),
                budget=int(self.config.lorebook.inject_budget_chars),
                max_entries=int(self.config.lorebook.max_entries),
                logger=self.ctx.logger,
            )
        # 慢变晋升机（批 4-C3/C4）：提案提炼器 + 确定性晋升状态机。
        # ⚠️ 不在这里 seed——seed 要调 LLM（最多 30s），会拖慢插件加载；
        # 放到看门狗首轮（见 _maybe_seed_perspective）。
        self._promotion = ProposalRunner(self)
        self._promotion_engine = PromotionEngine(self)
        # REPLY_EXTENSION 全名（批 4 / R39）：宿主 plugin_options 的键 = f"{plugin_id}.{name}"
        # （component_registry:116），id 以 _manifest.json 为准（版本号同源读取先例）
        self.reply_extension_full_name = f"{self._manifest_id()}.{_REPLY_EXTENSION_NAME}"
        # 依赖束完成装配（深化 B）：engine 构造时 streams / lorebook 等尚未建，
        # 此处补全快照——必须在 _reconcile_all 之前（tick 一启动就会读 deps）
        self._engine.rebind_deps()
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

    # OBSERVE(R14)：启动期锚定一致性比对——默认关但通道必须留着，宿主与插件双人格分裂时只有它能报警；
    # 任何异常只降级为 debug，绝不阻断插件加载。
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

    def _manifest_id(self) -> str:
        """从 _manifest.json 读插件 id（REPLY_EXTENSION full_name 组装用，批 4 / R39）。"""
        try:
            data = json.loads((Path(__file__).parent / "_manifest.json").read_text(encoding="utf-8"))
            plugin_id = str(data.get("id") or "").strip()
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("读取 _manifest.json id 失败: %s", exc)
            return ""
        if not plugin_id:
            self.ctx.logger.warning("_manifest.json 缺少 id 字段")
            return ""
        return plugin_id

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
        # 依赖束重绑定（深化 B）：宿主在本回调**之前**已装入新配置实例
        # （runner _handle_config_updated 先 set_plugin_config 后回调），
        # engine 必须换新快照，否则 tick / reconcile 永远读旧配置
        if self._engine is not None:
            self._engine.rebind_deps()
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
        # 提案提炼自己的节流判定时刻（是否在 penalty 间隔/退避窗内）
        now = self._local_now()

        result = await self._promotion.run(now=now)
        # ❗ 上面的提炼**含 LLM 调用**（实测约 25 秒），其后所有**写入**必须换用新鲜时刻：
        #    沿用入口的 now 会把 promotions.ts / chronicle.ts 记成「周期开始」而非
        #    「实际写入」，审计时间戳系统性偏早，无法与计数器、日志时间线对账。
        write_now = self._local_now()
        if str(result.get("status") or "") == "ok":
            # 反证先于晋升：被引用的旧提案先被驳回，免得它同一轮又被应用一次
            self._promotion_engine.apply_refutations(now=write_now)
            applied_any = False
            for row in self._store.list_proposals(status="pending", limit=200):
                if self._promotion_engine.apply(row, now=write_now).get("applied"):
                    applied_any = True
            if applied_any:
                # 有晋升落库才写回投影（E5：只投影 general 维度；relationship 绝不落盘）
                self._project_learned()

        self._maybe_promote_relationship(write_now)

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

    def _observed_group_ids(self) -> List[str]:
        """配置里登记的观察群号（规范化去重，空列表 = 观察关闭）。"""
        raw = getattr(self.config.narrative, "observe_group_ids", None) or []
        seen: Dict[str, None] = {}
        for item in raw:
            gid = str(item).strip()
            if gid:
                seen[gid] = None
        return list(seen.keys())

    def observed_group_ids(self) -> List[str]:
        """需要参与**事件清理**的群号（= 配置名单 ∪ 已知会话映射）。

        比 ``_observed_group_ids`` 多一份「已学过的群」：用户把某个群从名单里
        摘掉后，它积累的历史事件必须还能被扫到，否则变成永久孤儿数据
        （清道夫只有 tick 这一个入口）。
        """
        merged: Dict[str, None] = {gid: None for gid in self._observed_group_ids()}
        if self._group_streams is not None:
            for gid in self._group_streams.known_gids():
                if gid:
                    merged[gid] = None
        return list(merged.keys())

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
        """用户消息到达：登记会话、更新互动状态、采集指标（实现见 services/inbound.py；壳只做 **kwargs 整体透传——🔴 不拆键、不新增 try/except）。"""
        return await inbound.handle_inbound_message(self, **kwargs)
    def _observe_group(self, message: Dict[str, Any], stream_id: str) -> None:
        """R35 群聊纯观察落库（实现见 services/inbound.py）。"""
        return inbound.observe_group(self, message, stream_id)
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
        """bot 出站消息构建完成后记录采样与配对（实现见 services/outbound.py；壳只做 **kwargs 整体透传）。"""
        return await outbound.handle_post_send(self, **kwargs)
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
        """把自我层/支线层/编年史渲染成 item 追加进请求（实现见 services/inject.py；壳只做 **kwargs 整体透传——🔴 不拆键、不新增 try/except）。"""
        return await inject.inject_life_context(self, **kwargs)
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
        """漂移层调制 + 关系语境 + 文学授权注入（实现见 services/inject.py；壳只做 **kwargs 整体透传）。"""
        return await inject.inject_drift_style(self, **kwargs)
    # ===== Hook：表达学习隔离 =====  # OBSERVE(R11)：两个 abort 看着像无用中断，实为防群腔调进私聊剧本、防剧本素材进表达库的唯一闸门，删掉即双向污染

    @HookHandler(
        "expression.select.before_select",
        name="narrative_block_expression_select",
        description="剧本模式会话阻断表达学习注入（防稀释人设）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def block_expression_select(self, **kwargs: Any) -> Dict[str, Any]:
        """剧本模式会话阻断表达选择注入（实现见 services/inject.py）。"""
        return await inject.block_expression_select(self, **kwargs)
    @HookHandler(
        "expression.learn.before_upsert",
        name="narrative_block_expression_upsert",
        description="剧本模式会话阻断表达学习写入（防污染 expressions 表）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def block_expression_upsert(self, **kwargs: Any) -> Dict[str, Any]:
        """剧本模式会话阻断表达写入（实现见 services/inject.py）。"""
        return await inject.block_expression_upsert(self, **kwargs)
    # ===== REPLY_EXTENSION：主动轮由头进 replyer 指令位（批 4 / R39 / 宿主 1.3.5） =====
    # OBSERVE(R39)：宿主通道有实现无文档，合同以 src/plugin_runtime/host/reply_extensions.py 为准——
    # prepare 只允许返回 {"extra_prompt"}（:148）；扩展异常 = 整次 reply 失败（:193-204），
    # 故 handler 任何异常**全体自捕获**降级 {}（记 ERROR 不静默）；before_send 本批不使用
    # （出站不动）。仍依赖 planner 配合：模型须在 reply 调用里主动填 plugin_options（见 reason 教学）。

    @ReplyExtension(
        name=_REPLY_EXTENSION_NAME,
        description="主动开口回合：把本轮由头放进回复生成的额外要求（承接你想说的那件事）",
        chat_scope="private",
    )
    async def _reply_ext_proactive_bysource(self, **payload: Any) -> Dict[str, Any]:
        """reply 扩展：prepare 返回由头承接提醒（实现见 services/inject.py；扩展异常全体自捕获系宿主契约，随实现体保留）。"""
        return await inject.reply_ext_proactive_bysource(self, **payload)
    # ===== Command：/narrative =====

    @Command(
        "narrative",
        description="剧本人设系统管理命令：/narrative help 查看用法",
        pattern=r"^\s*/narrative(?:\s+(?P<sub>.+))?\s*$",
    )
    async def handle_narrative_command(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """管理命令分发：help / rollback / status / reset（实现见 services/commands.py；分发经 self._cmd_* 属性调用——测试 fake 以实例属性覆写）。"""
        return await commands.handle(self, **kwargs)
    def _is_admin(self, user_id: str) -> bool:
        """管理员判定（实现见 services/commands.py）。"""
        return commands.is_admin(self, user_id)
    async def _cmd_help(self, stream_id: str) -> None:
        """/narrative help（实现见 services/commands.py）。"""
        return await commands.cmd_help(self, stream_id)
    async def _cmd_rollback(self, param: str, stream_id: str) -> None:
        """/narrative rollback（实现见 services/commands.py）。"""
        return await commands.cmd_rollback(self, param, stream_id)
    async def _cmd_status(self, stream_id: str, audience: str = "") -> None:
        """/narrative status（实现见 services/commands.py）。"""
        return await commands.cmd_status(self, stream_id, audience)
    async def _cmd_reset(self, param: str, stream_id: str) -> None:
        """/narrative reset（实现见 services/commands.py）。"""
        return await commands.cmd_reset(self, param, stream_id)
    @API(
        "narrative_state",
        description="查询剧本状态摘要（模式/心情/关系/编年史计数，不含聊天正文）。",
        version="1",
        public=False,
    )
    async def handle_narrative_state_api(self, **kwargs: Any) -> Dict[str, Any]:
        """narrative_state API（实现见 services/apis.py）。"""
        return await apis.state_api(self, **kwargs)
    @API(
        "narrative_diary_context",
        description=(
            "供 mai-diary 等插件握手：返回剧本会话判定所需的用户/会话列表，"
            "以及自我层人格摘要（锚定 identity + 心情 + 作息 + 当日生活素材）。"
            "不含聊天正文；编年史片段有截断。可选入参 date=被写日记的日期"
            "（YYYY-MM-DD，diary 04:00 写的是昨天）。"
        ),
        version="1",
        public=True,
    )
    async def handle_narrative_diary_context_api(
        self, date: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """narrative_diary_context API（实现见 services/apis.py）。"""
        return await apis.diary_context_api(self, date=date, **kwargs)
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
        """narrative_chronicle_append API（实现见 services/apis.py）。"""
        return await apis.chronicle_append_api(
            self, date=date, content=content, audience=audience, **kwargs
        )
def create_plugin() -> MaiNarrativePlugin:
    """工厂函数：Runner 通过此函数实例化插件。"""
    return MaiNarrativePlugin()