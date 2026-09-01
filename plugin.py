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

import asyncio
import contextlib
import datetime
from typing import Any, Dict, List, Optional, Tuple

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
    Telemetry,
    build_context_block,
    build_injected_item,
    is_injected_item,
)
from .services.engine import local_now
from .services.store import NarrativeStore

# 用户消息与上一条 bot 消息的间隔超过该值，视为"用户主动发起"
_USER_INITIATED_GAP_MINUTES = 5
# 主动消息后 30 分钟内用户回复，记为"被接住"
_PROACTIVE_REPLY_WINDOW_MINUTES = 30
# 关系阶段阈值（familiarity，只进不退）
_STAGE_THRESHOLDS: List[Tuple[float, str]] = [
    (85, "挚友"),
    (60, "朋友"),
    (30, "熟人"),
]
_STAGE_EPOCH = datetime.datetime(1970, 1, 1)


class MaiNarrativePlugin(MaiBotPlugin):
    """剧本人设系统主插件。"""

    config_model: type[PluginConfigBase] = MaiNarrativePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[NarrativeStore] = None
        self._engine: Optional[NarrativeEngine] = None
        self._proactive: Optional[ProactiveScheduler] = None
        self._telemetry: Optional[Telemetry] = None
        # uid -> 已学到的私聊 stream_id；stream_id -> uid（反向映射用于 hook 判定）
        self._uid_to_stream: Dict[str, str] = {}
        self._stream_to_uid: Dict[str, str] = {}
        # stream_id -> 最后一次 bot 发送时刻（用户主动发起判定）
        self._last_bot_sent: Dict[str, datetime.datetime] = {}
        # uid -> 最近主动消息时刻列表（30 分钟回复判定）
        self._proactive_sent_at: Dict[str, List[datetime.datetime]] = {}
        # stream_id -> 最近主动消息触发时刻（渲染侧判断当前轮是否为主动开口轮）
        self._proactive_pending_at: Dict[str, datetime.datetime] = {}
        # 看门狗任务：不依赖 on_config_update 回调，主动对齐"配置开关 ↔ 后台任务"
        self._watchdog: Optional[asyncio.Task] = None

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("mai-narrative 已禁用（plugin.enabled=false），仅保留命令")
        data_dir = self.ctx.paths.data_dir / "narrative"
        self._store = NarrativeStore(data_dir)
        self._telemetry = Telemetry(self)
        self._engine = NarrativeEngine(self)
        self._proactive = ProactiveScheduler(self)
        await self._restart_tasks()
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="narrative-watchdog")
        self.ctx.logger.info(
            "mai-narrative v0.1 已加载（剧本=%s 主动=%s 模式用户=%s 数据目录=%s）",
            self.config.narrative.enabled,
            self.config.proactive.enabled,
            ",".join(self._mode_user_ids()) or "无",
            data_dir,
        )

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
        self.ctx.logger.info(
            "mai-narrative 配置更新: scope=%s version=%s（任务按新配置重排）",
            scope, version,
        )
        await self._restart_tasks()

    async def _watchdog_loop(self) -> None:
        """看门狗：每 15s 对齐"配置开关 ↔ 引擎/主动任务"。

        背景（真机实测）：WebUI 打开剧本开关后，``on_config_update`` 若未触发，
        引擎不会自行启动（阶段/精力冻结在旧值）。看门狗让引擎在被启用后的
        15s 内自动拉起，不依赖回调时序。
        """
        try:
            while True:
                try:
                    await self._ensure_tasks_aligned()
                except Exception as exc:
                    self.ctx.logger.warning("narrative 看门狗异常: %s", exc, exc_info=True)
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            pass

    async def _ensure_tasks_aligned(self) -> None:
        """按当前配置同步引擎/主动调度器的启停状态（幂等）。"""
        if self._engine is None or self._proactive is None:
            return
        cfg = self.config
        want_engine = bool(cfg.plugin.enabled and cfg.narrative.enabled)
        want_proactive = bool(want_engine and cfg.proactive.enabled)

        engine_running = bool(self._engine._running) if hasattr(self._engine, "_running") else False
        if want_engine and not engine_running:
            self.ctx.logger.info("narrative 看门狗：启动世界时钟（剧本已启用）")
            self._engine.start()
        elif not want_engine and engine_running:
            await self._engine.stop()
        elif not want_engine and not engine_running:
            self.ctx.logger.info(
                "narrative 看门狗：无动作（剧本开关 plugin=%s narrative=%s）",
                cfg.plugin.enabled, cfg.narrative.enabled,
            )

        proactive_running = bool(self._proactive._running) if hasattr(self._proactive, "_running") else False
        if want_proactive and not proactive_running:
            self.ctx.logger.info("narrative 看门狗：启动主动消息调度")
            self._proactive.start()
        elif not want_proactive and proactive_running:
            await self._proactive.stop()

    async def _restart_tasks(self) -> None:
        """按当前开关重启世界时钟与主动调度任务。"""
        if self._engine is None or self._proactive is None:
            return
        with contextlib.suppress(Exception):
            await self._engine.stop()
        with contextlib.suppress(Exception):
            await self._proactive.stop()
        if self.config.plugin.enabled and self.config.narrative.enabled:
            self._engine.start()
        if (
            self.config.plugin.enabled
            and self.config.narrative.enabled
            and self.config.proactive.enabled
        ):
            self._proactive.start()

    # ===== 消息辅助（字段细节与 always-reply-private 一致） =====

    def _mode_user_ids(self) -> List[str]:
        """剧本模式用户 ID 列表（规范化字符串）。"""
        return [str(item) for item in (self.config.narrative.mode_user_ids or []) if str(item).strip()]

    def _is_mode_uid(self, user_id: str) -> bool:
        """判断用户是否是剧本模式用户。"""
        return bool(user_id) and user_id in set(self._mode_user_ids())

    def _is_private_chat(self, message: Dict[str, Any]) -> bool:
        """判断消息是否为私聊。

        真机实测：本代 hook 载荷没有 is_private/chat_type 字段，主程序判定规则为
        ``chat_type = "group" if message_info.group_info else "private"``
        （src/chat/message_receive/message.py:61）——先按 group_info 判，再做旧字段兜底。
        """
        msg_info = message.get("message_info")
        if isinstance(msg_info, dict) and isinstance(msg_info.get("group_info"), dict):
            return False
        if isinstance(msg_info, dict) and msg_info.get("group_info") is None:
            return True
        if message.get("is_private") is True:
            return True
        chat_type = (
            message.get("chat_type")
            or message.get("scene")
            or message.get("detail_type")
            or ""
        )
        return str(chat_type) in ("private", "direct")

    def _message_text(self, message: Dict[str, Any]) -> str:
        """提取消息正文。

        真机实测：`raw_message` 是分段数组（[{"type":"text","data":...}]），
        `processed_plain_text` 才是拼好的纯文本，优先用它。
        """
        text = str(message.get("processed_plain_text") or "").strip()
        if text:
            return text
        raw = message.get("raw_message")
        if isinstance(raw, list):
            parts: List[str] = []
            for segment in raw:
                if isinstance(segment, dict):
                    data = segment.get("data")
                    parts.append(str(data) if data is not None else "")
                else:
                    parts.append(str(segment))
            return "".join(parts).strip()
        return str(raw or "").strip()

    def _extract_user_id(self, message: Dict[str, Any]) -> str:
        """从消息中提取用户 ID。"""
        user_id = str(message.get("user_id") or "").strip()
        if user_id:
            return user_id
        msg_info = message.get("message_info") or {}
        user_info = msg_info.get("user_info") or {}
        return str(user_info.get("user_id") or user_info.get("id") or "").strip()

    def _record_stream(self, user_id: str, stream_id: str) -> None:
        """登记 uid<->stream 映射（私聊主动开口与 hook 判定用）。"""
        if not user_id or not stream_id:
            return
        self._uid_to_stream[user_id] = stream_id
        self._stream_to_uid[stream_id] = user_id

    def _stream_id_of(self, user_id: str) -> str:
        """查询用户已知的私聊 stream_id。"""
        return self._uid_to_stream.get(user_id, "")

    def _is_mode_session(self, session_id: str) -> bool:
        """判断会话是否为剧本模式会话（显式白名单或已学习映射）。"""
        if not session_id:
            return False
        if session_id in (str(item) for item in (self.config.narrative.mode_stream_ids or [])):
            return True
        uid = self._stream_to_uid.get(session_id, "")
        return self._is_mode_uid(uid)

    def _consume_proactive_pending(self, session_id: str, window_seconds: int = 30) -> Tuple[str, str]:
        """主动轮判定：本会话 30s 内刚触发过主动消息 → 返回 ("proactive", 由头)。

        触发后由调度器写入 ``_proactive_pending_at``（结构 {ts, bysource}），
        本处消费一次即清除，避免把后续普通轮次误判为主动轮。
        Returns:
            (round_kind, bysource)：round_kind ∈ {"proactive","reply"}；
            bysource 非空仅当主动轮（由头文本，供渲染注入）。
        """
        entry = self._proactive_pending_at.pop(session_id, None)
        if entry is not None and (self._local_now() - entry["ts"]).total_seconds() <= window_seconds:
            return "proactive", str(entry.get("bysource", "") or "")
        return "reply", ""

    def _local_now(self) -> datetime.datetime:
        """按插件配置时区取本地时间（与引擎统一）。"""
        return local_now(self.config.narrative.timezone_offset_hours)

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

        带结构化日志：任一过滤分支命中都会留痕，便于真机定位
        （曾出现入站落痕整条链不生效、但注入 hook 正常的问题）。
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
            self.ctx.logger.info("narrative inbound: message 为空/非 dict（type=%s）", type(message).__name__)
            return {"action": "continue", "modified_kwargs": kwargs}

        user_id = self._extract_user_id(message)
        is_private = self._is_private_chat(message)
        is_mode = self._is_mode_uid(user_id)
        self.ctx.logger.info(
            "narrative inbound: keys=%s | user_id=%r | stream_id=%r | is_private=%s | is_mode=%s | text=%s",
            sorted(message.keys()), user_id, stream_id, is_private, is_mode,
            self._message_text(message)[:30],
        )
        if not is_private:
            return {"action": "continue", "modified_kwargs": kwargs}
        if not is_mode:
            self.ctx.logger.info("narrative inbound: 用户不在模式名单，uid=%r", user_id)
            return {"action": "continue", "modified_kwargs": kwargs}

        if stream_id:
            self._record_stream(user_id, stream_id)

        # 命令/通知类消息不进剧本素材（命令是"你本人操作"，不是 bot 的生活）
        if bool(message.get("is_command")) or bool(message.get("is_notify")):
            self.ctx.logger.info("narrative inbound: 命令/通知消息（is_command=%s is_notify=%s），跳过素材采集 uid=%s",
                                 message.get("is_command"), message.get("is_notify"), user_id)
            return {"action": "continue", "modified_kwargs": kwargs}

        plain = self._message_text(message)
        now = self._local_now()
        self._engine.record_interaction(user_id, plain, now)
        self._update_branch_feedback(user_id, now)

        # 验收指标 1：用户主动发起（距上一条 bot 消息超过阈值）
        last_sent = self._last_bot_sent.get(stream_id or user_id, _STAGE_EPOCH)
        if (now - last_sent).total_seconds() / 60 > _USER_INITIATED_GAP_MINUTES:
            self._telemetry.record("user_initiated_freq", 1, user_id=user_id)
        # 验收指标 2：入站消息长度
        self._telemetry.record("dialogue_depth", value=float(len(plain)), user_id=user_id, scope="user_msg_len")
        # 验收指标 3：主动消息是否被接住
        if self._check_proactive_reply(user_id, now):
            self._telemetry.record("proactive_replied", 1, user_id=user_id)
        self.ctx.logger.info("narrative inbound: 落痕完成 uid=%s stream=%s", user_id, stream_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    def _update_branch_feedback(self, user_id: str, now: datetime.datetime) -> None:
        """支线层反馈：信任/熟悉度小步增长；里程碑只进不退。"""
        if self._engine is None:
            return
        branch = self._engine.load_branch_state(user_id)
        inner = branch["state"]
        inner["last_interaction_ts"] = now.isoformat(timespec="seconds")
        inner["familiarity"] = round(min(100.0, float(inner.get("familiarity", 0.0)) + 0.8), 1)
        inner["trust"] = round(min(100.0, float(inner.get("trust", 0.0)) + 0.5), 1)

        stage = str(branch["identity"].get("stage", "陌生人"))
        familiarity = float(inner["familiarity"])
        milestones = list(inner.get("milestones", []))
        for threshold, label in _STAGE_THRESHOLDS:
            if familiarity >= threshold and stage != label:
                old_stage = stage
                branch["identity"]["stage"] = label
                if not any(item.get("id") == f"stage:{label}" for item in milestones):
                    milestones.append(
                        {
                            "id": f"stage:{label}",
                            "ts": now.isoformat(timespec="seconds"),
                            "desc": f"你们从{old_stage}变成了{label}",
                            "stage": "done",
                        }
                    )
                inner["milestones"] = milestones
                break
        self._engine.save_branch_state(user_id, branch)

    def _check_proactive_reply(self, user_id: str, now: datetime.datetime) -> bool:
        """主动消息 30 分钟内收到用户回复 → 记一次"被接住"。"""
        sent_list = self._proactive_sent_at.get(user_id, [])
        if not sent_list:
            return False
        active = [item for item in sent_list if (now - item).total_seconds() / 60 <= _PROACTIVE_REPLY_WINDOW_MINUTES]
        self._proactive_sent_at[user_id] = active[-2:]
        return bool(active)

    # ===== 出站 Hook：采样（受入站事件不派发影响，出站同样改用命名 hook） =====

    @HookHandler(
        "send_service.before_send",
        name="narrative_post_send",
        description="剧本模式出站采样（对话深度/成本）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_post_send(self, **kwargs: Any) -> Dict[str, Any]:
        """bot 发送消息前：记录出站时刻与长度（指标 1/2 的对照侧）。"""
        message = kwargs.get("message")
        resolved_stream = str(kwargs.get("stream_id") or kwargs.get("session_id") or "")
        if self._telemetry is None:
            return {"action": "continue", "modified_kwargs": kwargs}
        if resolved_stream:
            self._last_bot_sent[resolved_stream] = self._local_now()
        if isinstance(message, dict):
            plain = str(message.get("plain_text") or message.get("raw_message") or "").strip()
            self._telemetry.record("dialogue_depth", value=float(len(plain)), scope="bot_msg_len")
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

        user_id = self._stream_to_uid.get(session_id, "")
        state = self._engine.load_self_state()
        branch = self._engine.load_branch_state(user_id) if user_id else None
        recent = self._store.recent_chronicle("self", limit=3)
        round_kind, bysource = self._consume_proactive_pending(session_id)
        context_text = build_context_block(
            self, state, branch, self._local_now(), recent, round_kind=round_kind, bysource=bysource
        )
        items.append(build_injected_item(context_text))
        kwargs["items"] = items
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
            await self._cmd_status(stream_id)
            return True, "ok", True
        if command == "reset":
            await self._cmd_reset(param, stream_id)
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
            "说明：配置在 WebUI 插件页修改（[identity] 锚定层人设请手动填写）。"
        )
        await self.ctx.send.text(text, stream_id)

    async def _cmd_status(self, stream_id: str) -> None:
        """状态摘要（不含聊天正文）。"""
        if self._engine is None or self._store is None:
            await self.ctx.send.text("插件尚未初始化完成，请稍后再试", stream_id)
            return
        cfg = self.config
        state = self._engine.load_self_state()
        inner = state["state"]
        lines = [
            "【剧本人设系统 · 状态】",
            f"剧本模式: {'开' if cfg.narrative.enabled else '关'} | "
            f"主动消息: {'开' if cfg.proactive.enabled else '关'}",
            f"模式用户: {','.join(self._mode_user_ids()) or '无'} | "
            f"已知会话: {len(self._uid_to_stream)}",
            f"心情: {inner['mood']['label']}（精力 {inner['mood']['energy'] * 10:.0f}/10）| "
            f"阶段: {inner['routine']['phase']}",
        ]
        hot = str(inner["focus"].get("hot_thread", "")).strip()
        if hot:
            lines.append(f"聚焦: {hot[:40]}")
        for uid in self._mode_user_ids():
            branch = self._engine.load_branch_state(uid)
            lines.append(
                f"支线[{uid}]: {branch['identity']['stage']} | "
                f"熟悉 {branch['state']['familiarity']:.0f} | 信任 {branch['state']['trust']:.0f}"
            )
        recent = self._store.recent_chronicle("self", limit=2)
        if recent:
            lines.append("编年史最近: " + str(recent[0].get("text", ""))[:40])
        today = self._local_now().strftime("%Y-%m-%d")
        for uid in self._mode_user_ids():
            count = self._store.get_kv_int(f"proactive:count:{uid}:{today}")
            lines.append(f"今日主动[{uid}]: {count}")
        lines.append(f"数据目录: {self.ctx.paths.data_dir / 'narrative'}")
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
        self._uid_to_stream.clear()
        self._stream_to_uid.clear()
        self._proactive_sent_at.clear()
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
            "known_streams": len(self._uid_to_stream),
            "chronicle_count": self._store.count_chronicle("self"),
        }
        summary["branches"] = {
            uid: {
                "stage": self._engine.load_branch_state(uid)["identity"]["stage"],
                "familiarity": self._engine.load_branch_state(uid)["state"]["familiarity"],
                "trust": self._engine.load_branch_state(uid)["state"]["trust"],
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
        traits = [str(item).strip() for item in (identity.immutable_traits or []) if str(item).strip()]
        expression_hint = "；".join(traits)

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
                "hot_thread": str(inner["focus"].get("hot_thread", "")),
                "latest_life_fragment": (
                    str(
                        list(inner.get("focus", {}).get("pending_events", []))[-1]
                        .get("text", "")
                    )[:60]
                    if inner.get("focus", {}).get("pending_events")
                    else ""
                ),
                "recent_chronicle": [
                    str(entry.get("text", ""))[:60]
                    for entry in self._store.recent_chronicle("self", limit=3)
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
        ),
        version="1",
        public=True,
    )
    async def handle_narrative_chronicle_append_api(
        self, date: str = "", content: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """把当日日记成品写入编年史（append-only，幂等）。"""
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

        written = self._store.append_chronicle_once("self", "diary", content, date)
        return {
            "ok": True,
            "written": written,
            "date": date,
            "reason": "" if written else "duplicate",
        }


def create_plugin() -> MaiNarrativePlugin:
    """工厂函数：Runner 通过此函数实例化插件。"""
    return MaiNarrativePlugin()