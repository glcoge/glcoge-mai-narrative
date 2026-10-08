"""主动消息调度：活跃窗口 + 随机间隔计时器 + 静默时段 + 每日上限 + 由头签发。

设计共识（grill 会话 Q4）：
- 主动开口必须"有由头"（由头来自剧本状态机，禁止干聊"在吗"）。
- 每用户活跃窗口内用随机区间计时器决定"何时"开口；窗口外/静默期绝不打扰。
- 出站走 Maisaka 原生 proactive 通道，bot 带着剧本上下文完成一轮主动对话。
"""

from __future__ import annotations

import asyncio
import datetime
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..kvkeys import PROACTIVE_COUNT as _PROACTIVE_COUNT_KEY
from ..learning.topic import note_pending_topic, reward_pending_topic
from ..proactive import sourcing
from ..state.continuity import append_milestone
from ..state.engine import parse_clock

# 承接窗口 = 冷落窗口（2026-09-22 定案，取代原先 30min / 24h 双口径）**16 小时**。
#
# 依据（全周期 1152 条聊天记录实测）：承接延迟的中位数是 4.2 小时，用 30 分钟做
# 窗口会让约六成的成功承接先被判成"冷落"、罚完才发生——这是分享欲净下降的
# 结构性来源，与由头质量、送达率都无关。故统一为**单一窗口**：
#   窗口内被接住 → caught(+gain)；窗口内无人接住 → ignored(-decay)。
# 窗口内外的口径（30min / 2h / 6h / 16h）由分析层对延迟分钟做筛选得出，
# 插件里只保留这一个阈值。
_PROACTIVE_CATCH_WINDOW_MINUTES = 16 * 60

# REPLY_EXTENSION 由头回查窗（批 4 / R39）：主动→reply 的真实间隔是秒级，
# 10 分钟为富余上限；超窗的由头不再提醒（prepare 返回 {}，防陈旧提醒）。
_REPLY_EXT_WINDOW_MINUTES = 10

# 每用户保留的主动开口记录条数上限（有界，防内存无增长界）
_SENT_KEEP_PER_USER = 10

# 送达确认宽限期（分钟）：只认领这期间内触发的开口，避免主动轮里 bot 连发多条时
# 把更早一次开口误标成"这次送达了"。
_DELIVER_CONFIRM_GRACE_MINUTES = 10

# share_urge（v0.1.8 第一步）采样未过后的重试间隔范围（分钟）：短延迟重试而非
# 重置完整随机间隔，避免分享欲高时错过整段活跃窗口；也不密集轮询骚扰判定。
# 第一步暂不入 config（对行为影响小），后续按真机数据再定。
_URGE_RETRY_RANGE = (30, 60)

# engaged 计数窗（方案 §6.1 / Q9=c / OBSERVE(P21)）：主动消息被接住后开一窗，
# 窗内用户回应 ≥3 条【且】合计 ≥30 字 才算「聊起来了」，达标记一条里程碑（原文，不总结）。
# 「嗯」式敷衍只贡献 replies=1/chars=1，两条也拿不下 30 字门槛 —— 这正是 Q9 的目的。
#
# ❗ 内存态、不持久化（有意为之）：engaged 描述的是**一次连贯的聊得起劲**，
# 跨进程续窗会把两段无关对话接成一段，反而错记。
# ❗ 三值均为模块常量（方案 §15.1 的 3+9 拆分）：观察期内物理上改不动阈值。
_ENGAGED_WINDOW_MINUTES = 60
_ENGAGED_REPLY_MIN = 3
_ENGAGED_CHARS_MIN = 30

# ISO 星期取值：1=周一 … 7=周日（与 datetime.isoweekday() 对齐）
_VALID_WEEKDAYS = frozenset({"1", "2", "3", "4", "5", "6", "7"})


def parse_window(value: str) -> Optional[Tuple[datetime.time, datetime.time]]:
    """解析窗口字符串 ``HH:MM-HH:MM``；失败返回 None。"""
    try:
        start_text, _, end_text = str(value or "").partition("-")
        start = parse_clock(start_text)
        end = parse_clock(end_text)
        if start is None or end is None:
            return None
        return start, end
    except (TypeError, ValueError):
        return None


def in_silent(now: datetime.time, silent_start: str, silent_end: str) -> bool:
    """静默判断：支持跨天区间（如 23:00-08:00）。"""
    start = parse_clock(silent_start)
    end = parse_clock(silent_end)
    if start is None or end is None:
        return False
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _in_window(now: datetime.time, start: datetime.time, end: datetime.time) -> bool:
    """单个区间判定，支持跨天（如 20:20-05:00）：右开区间。"""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def in_windows(now: datetime.time, windows: List[str]) -> bool:
    """是否落在任一活跃窗口内（无星期维度，供 default_active_window 使用）。"""
    for window_text in windows:
        window = parse_window(window_text)
        if window is None:
            continue
        if _in_window(now, window[0], window[1]):
            return True
    return False


def rule_matches_now(now: datetime.datetime, rule: Any) -> bool:
    """按用户规则是否命中当前时刻：先判星期，再判时间窗（支持跨天）。

    星期按**当前时刻**判定（2026-09-14 决策）：周五 20:20 起的窗口过了 24:00 即
    失效，不再算作周五规则的延续——否则会污染"周末照常"这类需求。
    ``days`` 为空 → 永不命中（配合"有规则即覆盖默认"实现"永不主动"，修复旧版
    "空列表反而回退默认窗口"的反直觉行为）。
    """
    days = {str(day).strip() for day in (getattr(rule, "days", None) or [])}
    if str(now.isoweekday()) not in days:
        return False
    window = parse_window(f"{getattr(rule, 'start', '')}-{getattr(rule, 'end', '')}")
    if window is None:
        return False
    return _in_window(now.time(), window[0], window[1])


def validate_rules(rules: Any) -> List[str]:
    """校验按用户窗口规则，返回人可读的错误列表（空列表 = 全部合法）。

    用于启动期 WARN：非法条目运行期会被跳过，但必须说清"哪一条、错在哪"——
    静默失效是最贵的失败模式（参考 ``[llm].creation_task`` 教训）。
    """
    errors: List[str] = []
    for index, rule in enumerate(rules or [], start=1):
        user_id = str(getattr(rule, "user_id", "") or "").strip()
        if not user_id.isdigit():
            errors.append(f"第 {index} 条: QQ 号必须是纯数字（当前 {user_id!r}）")
        bad_days = [
            str(day).strip()
            for day in (getattr(rule, "days", None) or [])
            if str(day).strip() not in _VALID_WEEKDAYS
        ]
        if bad_days:
            errors.append(
                f"第 {index} 条: 星期取值非法 {bad_days}（只可填 1-7，1=周一、7=周日）"
            )
        start = str(getattr(rule, "start", "") or "").strip()
        end = str(getattr(rule, "end", "") or "").strip()
        if parse_window(f"{start}-{end}") is None:
            errors.append(
                f"第 {index} 条: 时刻非法（开始 {start!r} / 结束 {end!r}，应为 HH:MM）"
            )
    return errors


@dataclass
class _SentRecord:
    """一次主动开口的完整生命周期记录。

    2026-09-22 定案：取代原先 ``_sent_at``（30min）+ ``_sent_long``（24h）两条并行
    队列。两条队列必然要回答"这一条算谁的"，而这个问题每回答一次就可能答错一次
    （当天上午刚答错过一次方向）。改为**一条记录 + 连续量**：

    - ``delivered``：出站侧确认真的发出去了（未送达的不进承接率分母，也不罚冷落）；
    - ``consumed``：已被一次承接结算掉（每条至多结算一次，天然防重复计数）；
    - 延迟分钟由 ``resolve_catch`` 在结算时算出并交给分析层，插件内无第二个阈值。
    """

    ts: datetime.datetime
    stream_id: str
    delivered: bool = False
    consumed: bool = False
    # 侧信道由头（渲染层消费），不参与任何指标判定
    bysource: str = field(default="")
    # 兑现回执署名（批 0 / R41）：签发时随 build_bysource_detail 落下，
    # 承接结算时落 receipt_catch（scope=origin）。不参与承接判定本身。
    event_id: str = field(default="")
    origin: str = field(default="")


@dataclass
class _EngagedWindow:
    """一次承接之后的「聊起来了」计数窗（进程内内存态，随 tick 回收）。

    与 ``_SentRecord`` 的分工：前者记**主动开口**的生命周期（送达/承接/冷落），
    本结构记**用户回应**的连续性（几条、多少字）。两者互不引用——
    「值不值得当回忆」不该由「发没发出去」推导。
    """

    opened: datetime.datetime
    replies: int = 0
    chars: int = 0
    #: 窗内用户原话（按时间顺序）；达标时拼接为里程碑 desc
    texts: List[str] = field(default_factory=list)


def with_reply_extension_hint(reason: str, full_name: str, enabled: bool) -> str:
    """reason 追加 REPLY_EXTENSION 选择教学（批 4 / R39；enabled 且有全名时才教）。

    教模型在 reply 调用里主动填 ``plugin_options={"<full_name>": {}}``——
    宿主 1.3.5 通道会把 prepare 的 extra_prompt 放进「【额外回复要求】」块
    （排在目标消息块之后的指令位）。这是"仍依赖 planner 配合"的那一层。
    """
    if not enabled or not full_name:
        return reason
    return (
        f"{reason}\n"
        f'调用 reply 工具时，请在参数里同时填 plugin_options={{"{full_name}": {{}}}}——'
        "这会把你的由头放进回复生成的额外要求，帮助回复贴合你想说的事。"
    )


def _trigger_accepted(result: Any) -> bool:
    """主动任务是否真的被主程序接受（用于区分"已排队"与"白跑一趟"）。

    宿主正常返回 ``{"stream_id", "task_id", "queued": True}``；被拒时可能返回
    ``success=False`` 或缺少 ``task_id``/``queued``。**不把 None 当成功**——异常
    已被 ``_fire`` 的 except 捕获并置为 None，此处只做显式判定。
    """
    if not isinstance(result, dict):
        return False
    if result.get("success") is False:
        return False
    return bool(result.get("queued")) or bool(result.get("task_id"))


class ProactiveScheduler:
    """主动消息调度器：以 asyncio 周期任务驱动。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # uid -> 下一次开口时刻；窗口外或已用完上限时为 None
        self._next_fire: Dict[str, datetime.datetime] = {}
        # uid -> 主动开口记录（承接判定 / 冷落结算 / 送达确认共用一份）
        self._sent_records: Dict[str, List[_SentRecord]] = {}
        # uid -> engaged 计数窗（承接命中开窗，达标即销毁；进程内内存态）
        self._engaged_windows: Dict[str, _EngagedWindow] = {}
        # stream_id -> 最近主动消息触发时刻（渲染侧判断当前轮是否为主动开口轮）
        self._pending_at: Dict[str, Dict[str, Any]] = {}

    # ─── 生命周期 ────────────────────────────────────────────────

    def start(self) -> None:
        """启动调度循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="narrative-proactive")

    async def stop(self) -> None:
        """停止调度循环。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def reconcile(self) -> None:
        """按当前配置幂等对齐启停状态（供 plugin 看门狗/配置热重载调用）。"""
        cfg = self._plugin.config
        want = bool(cfg.plugin.enabled and cfg.narrative.enabled and cfg.proactive.enabled)
        if want and not self._running:
            self.start()
        elif not want and self._running:
            await self.stop()

    # ─── 主循环 ──────────────────────────────────────────────────

    async def _loop(self) -> None:
        """每 30 秒检查一次待开口用户。"""
        try:
            while self._running:
                try:
                    await self._check_once()
                except Exception as exc:
                    self._plugin.ctx.logger.error("主动消息调度异常: %s", exc, exc_info=True)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    async def _check_once(self) -> None:
        """单轮检查：窗口/静默/上限 → 是否到点 → 触发。"""
        cfg = self._plugin.config
        if not cfg.plugin.enabled or not cfg.narrative.enabled or not cfg.proactive.enabled:
            self._next_fire.clear()
            return

        now = self._plugin._local_now()
        for user_id in (cfg.narrative.mode_user_ids or []):
            stream_id = self._plugin._streams.stream_of(user_id)
            if not stream_id:
                continue

            # 窗口到期结算（share_urge）：超窗无人接住 → 按条罚一次冷落。
            # 只罚**已确认送达**的——没发出去的消息用户根本没看到，罚它只会让
            # 分享欲无谓下跌（v0.1.8 沉默螺旋的一部分就来自这里）。未送达的
            # 单列 undelivered 供漏斗分析。放在窗口/上限检查之前：即便已超日
            # 上限或不在窗口，冷落反馈照样生效。
            ignored, undelivered = self.settle_expired(user_id, now)
            for _ in range(ignored):
                sourcing.record_urge_feedback(self._plugin._engine.deps, user_id, "ignored")
            for _ in range(undelivered):
                self._plugin._telemetry.record(
                    "proactive_undelivered", 1, user_id=user_id, scope="proactive"
                )

            today = now.strftime("%Y-%m-%d")
            day_count = self._plugin._engine.deps.store.get_kv_int(f"proactive:count:{user_id}:{today}")
            if day_count >= max(0, int(cfg.proactive.daily_max)):
                self._next_fire.pop(user_id, None)
                continue

            if in_silent(now.time(), cfg.proactive.silent_start, cfg.proactive.silent_end):
                self._next_fire.pop(user_id, None)
                continue

            # 睡眠闸门（v0.1.10）：与静默期**取并集**——睡着我也不找人。
            # 两者不合并成一个旋钮：静默期是「我不想主动开口」的独立收紧项，
            # 睡眠态还额外管创作层、精力与对话语气。任一命中即不开口。
            # （当前默认值下睡眠窗口 23:30-07:00 ⊂ 静默期 23:00-08:00，本闸门是冗余保险；
            #   一旦把静默期调窄或把睡眠窗口调宽，它才真正生效。）
            if self._plugin._engine.is_asleep(now):
                self._next_fire.pop(user_id, None)
                continue

            if not self._allowed_now(user_id, now):
                self._next_fire.pop(user_id, None)
                continue

            next_at = self._next_fire.get(user_id)
            if next_at is None:
                low, high = self._random_range()
                self._next_fire[user_id] = now + datetime.timedelta(minutes=random.randint(low, high))
                continue

            if now < next_at:
                continue

            # share_urge 采样（v0.1.8 第一步）：计时器到点只是"最小间隔闸门"，
            # 真正开口还要看此刻想不想说（动机驱动时机）。未过 → 短延迟重试。
            urge = sourcing.compute_share_urge(self._plugin._engine.deps, user_id)
            if random.random() >= urge:
                low, high = _URGE_RETRY_RANGE
                self._next_fire[user_id] = now + datetime.timedelta(minutes=random.randint(low, high))
                self._plugin.ctx.logger.debug(
                    "主动消息分享欲未过: uid=%s urge=%.2f → %d-%d 分钟后重试",
                    user_id, urge, low, high,
                )
                continue

            self._next_fire[user_id] = None
            await self._fire(user_id, stream_id, now)

    # ─── 触发 ────────────────────────────────────────────────────

    async def _fire(self, user_id: str, stream_id: str, now: datetime.datetime) -> None:
        """签发由头并触发 Maisaka 主动任务。由头为空（无可借生活素材）则跳过本轮。"""
        cfg = self._plugin.config
        plugin = self._plugin
        # 批 0（R41）：改用 detail 版取事件实体署名（event_id/origin 落回执）——
        # 取材与选择逻辑同一实现，text 与旧接口逐字节一致（零行为 diff）。
        bysource_detail = sourcing.build_bysource_detail(plugin._engine.deps, user_id, now)
        if not bysource_detail:
            plugin.ctx.logger.info(
                "主动消息跳过: uid=%s stream=%s 无可借由的生活素材（不干聊）", user_id, stream_id
            )
            return
        bysource = str(bysource_detail.get("text", ""))
        intent = "按剧本生活主动开口"
        # msg_id 硬要求（2026-09-22）：主程序 reply 工具强制要求一个上下文里真实
        # 存在的 msg_id，而主动开口没有"被回复的那条消息"，模型极易漏传——实测
        # 68 次主动 reply 里漏传 11 次，占 09-09 之后全部失败的 100%（送达率被
        # 卡在 76%）。主程序已在主动回合前回填真实用户消息（runtime.py:372），
        # 锚点是有的，缺的只是把要求讲明白。
        # ⚠️ 陷阱：主动任务自身带 id="proactive:<plugin>:<ts>"（runtime.py:679），
        # 模型会误当成 msg_id 抄——已实证 2 次，故在此点名禁止。
        reason = (
            f"{bysource}\n"
            "（开口时必须调用 reply 工具并传入 msg_id：取上下文里最近一条用户消息"
            '前缀中的 msg_id="..." 数字；不要使用 proactive: 开头的 id，那不是可回复的消息。）'
        )
        # 批 4（R39）：教模型选择 REPLY_EXTENSION（enabled 时；full_name 由 on_load 组装）
        try:
            pro_cfg = getattr(cfg, "proactive", None)
            reason = with_reply_extension_hint(
                reason,
                str(getattr(plugin, "reply_extension_full_name", "") or ""),
                bool(getattr(pro_cfg, "reply_extension_enabled", False)),
            )
        except Exception as exc:
            plugin.ctx.logger.warning("reply 扩展教学拼接失败（不影响触发）: %s", exc)
        plugin.ctx.logger.info(
            "主动消息触发: uid=%s stream=%s 由头=%s", user_id, stream_id, bysource
        )
        try:
            result = await plugin.ctx.maisaka.proactive.trigger(
                stream_id,
                intent=intent,
                reason=reason,
                metadata={"source": "glcoge.mai-narrative", "user_id": user_id},
            )
        except Exception as exc:
            plugin.ctx.logger.warning("主动消息触发失败: %s", exc)
            result = None
        if not _trigger_accepted(result):
            # 触发被拒（流不存在 / 被限流 / 返回异常）却不记账，会让 proactive_sent
            # 凭空虚增——分母里混进压根没排上队的轮次。
            plugin.ctx.logger.warning(
                "主动消息触发被拒: uid=%s stream=%s result=%r", user_id, stream_id, result
            )
            plugin._telemetry.record(
                "proactive_trigger_failed", 1, user_id=user_id, scope="proactive"
            )
            return

        today = now.strftime("%Y-%m-%d")
        day_count = plugin._store.get_kv_int(f"proactive:count:{user_id}:{today}")
        plugin._store.set_kv_int(f"proactive:count:{user_id}:{today}", day_count + 1)
        self.record_sent(
            user_id,
            stream_id,
            now,
            bysource,
            event_id=str(bysource_detail.get("event_id", "")),
            origin=str(bysource_detail.get("origin", "")),
        )
        # 话题归因（批 3-C5 / R16，ADR-0003 §7）：记下本轮讲的是什么话题，
        # 等用户接话时结算为主证据权重（P10）
        note_pending_topic(plugin._store, user_id, bysource)
        plugin._telemetry.record("proactive_sent", 1, user_id=user_id, scope="proactive")

    def record_sent(
        self,
        user_id: str,
        stream_id: str,
        now: datetime.datetime,
        bysource: str,
        event_id: str = "",
        origin: str = "",
    ) -> None:
        """登记一次主动开口。**此时尚未确认送达**，delivered 由出站侧回填。

        ``event_id``/``origin``（批 0 / R41）：由头的事件实体署名，承接结算时
        随 receipt_catch 落指标（scope=origin）；缺省空串——旧签名调用零影响。
        """
        records = self._sent_records.setdefault(user_id, [])
        records.append(
            _SentRecord(ts=now, stream_id=stream_id, bysource=bysource, event_id=event_id, origin=origin)
        )
        self._sent_records[user_id] = records[-_SENT_KEEP_PER_USER:]
        self._pending_at[stream_id] = {
            "ts": now,
            "bysource": bysource,
        }

    def mark_delivered(self, stream_id: str, now: Optional[datetime.datetime] = None) -> bool:
        """出站确认：该会话最近一次未确认送达的主动开口，确实发出去了。

        只有被标记的记录才会计入 ``proactive_delivered``（承接率的真分母），
        也只有它才能被承接、才会因无人回应而罚冷落。返回是否命中了一条待确认记录。

        ``now`` 用于**宽限期判定**：只认领最近 ``_DELIVER_CONFIRM_GRACE_MINUTES``
        分钟内触发的开口。主动轮里 bot 可能连发多条，若不设宽限，第二条出站消息
        会把更早一次（早已结算完毕的）开口误标成"这次送达了"。
        """
        if not stream_id:
            return False
        latest: Optional[_SentRecord] = None
        for records in self._sent_records.values():
            for rec in records:
                if rec.stream_id != stream_id or rec.delivered:
                    continue
                if latest is None or rec.ts > latest.ts:
                    latest = rec
        if latest is None:
            return False
        if now is not None and (now - latest.ts).total_seconds() > _DELIVER_CONFIRM_GRACE_MINUTES * 60:
            return False
        latest.delivered = True
        return True

    def consume_pending(self, session_id: str, window_seconds: int = 30) -> Tuple[str, str]:
        """主动轮判定：本会话 window_seconds 内刚触发过主动消息 → 返回 ("proactive", 由头)。

        触发后由本调度器写入 ``_pending_at``，本处消费一次即清除，避免把后续普通轮次误判为主动轮。

        Returns:
            (round_kind, bysource)：round_kind ∈ {"proactive","reply"}；
            bysource 非空仅当主动轮（由头文本，供渲染注入）。
        """
        entry = self._pending_at.pop(session_id, None)
        if entry is not None and (self._plugin._local_now() - entry["ts"]).total_seconds() <= window_seconds:
            return "proactive", str(entry.get("bysource", "") or "")
        return "reply", ""

    def bysource_for_reply(self, session_id: str, now: datetime.datetime) -> str:
        """REPLY_EXTENSION prepare 用：该会话最近一次主动开口的由头（批 4 / R39）。

        ⚠️ 不能复用 ``_pending_at``——它在注入 hook 的 ``consume_pending`` 已被
        消费清除；本处读 ``_sent_records``（``record_sent`` 落下，含
        stream_id/ts/bysource），窗口 ``_REPLY_EXT_WINDOW_MINUTES``（主动→reply
        间隔秒级，10 分钟富余）。内存态：重启丢 = 本轮无由头提醒（可接受，
        与 engaged 窗同哲学）。
        """
        latest_text = ""
        latest_ts: Optional[datetime.datetime] = None
        for records in self._sent_records.values():
            for rec in records:
                if rec.stream_id != session_id:
                    continue
                if (now - rec.ts).total_seconds() > _REPLY_EXT_WINDOW_MINUTES * 60:
                    continue
                if latest_ts is None or rec.ts > latest_ts:
                    latest_ts = rec.ts
                    latest_text = rec.bysource
        return latest_text

    def resolve_catch(self, user_id: str, now: datetime.datetime) -> Optional[float]:
        """入站承接结算：返回本次承接的**延迟分钟数**，无可结算记录则返回 None。

        取**最近一条**未被承接、且仍在窗口内的主动消息，命中即标记 ``consumed`` ——
        每条主动消息至多结算一次，所以不存在"两个口径抢同一条"的问题。

        ⚠️ **只归属已确认送达的开口**（2026-09-22 离线回放实证，见
        ``analysis/17-replay.txt``）：不设这个门槛时，用户本来就在聊天、随手发的
        下一条消息会被贪心规则错记成"对我们主动开口的回应"，回放里因此多出
        16 条假承接，承接率 50/44 = **114%**。加上门槛后为 34/44 = 77%，与手算
        基准 78% 吻合。

        返回的是连续量而非布尔值：30 分钟 / 2 小时 / 6 小时 / 16 小时这些口径
        全部由分析层对延迟分钟做筛选得出，插件里不再维护第二个计数器。

        延迟为负（时钟回拨 / 由头时间戳异常）时按 0 计，不产生负延迟样本。
        """
        records = self._sent_records.get(user_id, [])
        for rec in reversed(records):
            if rec.consumed or not rec.delivered:
                continue
            elapsed = (now - rec.ts).total_seconds() / 60
            if elapsed > _PROACTIVE_CATCH_WINDOW_MINUTES:
                # 最近的这条已超窗，更早的只会更旧
                break
            rec.consumed = True
            # 话题归因（批 3-C5 / R16）：接住 → 本轮话题记主证据权重（P10=1.0），
            # 生活片段自身主题只有 0.4（降权），长期看用户爱聊的会占主导
            reward_pending_topic(self._plugin._engine.deps.store, user_id)
            # 兑现回执（批 0 / R41 / 执行路线 §批 0-4）：**只记指标，不做一致性约束**
            # ——由头被岔开 = 特性（总览 §5.1），不写回状态、不罚不发。
            # scope=origin 供供给质量分析（批 2 后可比 fragment vs seed 接住率）。
            # 走通用 record 通道而非 record_counter（后者是晋升链路专属，R17）。
            # getattr 守卫沿用 life.py:153 的可选子系统取用先例（旧测试夹具不带 _telemetry）。
            telemetry = getattr(self._plugin, "_telemetry", None)
            if telemetry is not None:
                telemetry.record(
                    "receipt_catch",
                    max(0.0, elapsed),
                    user_id=user_id,
                    scope=rec.origin,
                )
            return max(0.0, elapsed)
        return None

    # ─── engaged 计数窗（方案 §6.1 / Q9=c / P21） ─────────────────

    def note_engaged(
        self,
        user_id: str,
        text: str,
        now: datetime.datetime,
        *,
        catch: bool = False,
    ) -> bool:
        """把一条用户消息计入 engaged 窗；返回本次是否**刚好达标**（落了一条里程碑）。

        - ``catch=True``：本条是承接命中的消息（``resolve_catch`` 返回非 None），
          无窗则开窗并计入本条（replies=1）；**已有窗时不重置**——一次连贯的对话
          可能包含多次承接，接着数即可，不必切成两个窗。
        - 无窗且 ``catch=False`` → 直接返回 False：窗外的普通消息与「回忆」无关，
          绝不自行开窗（回忆必须由一次真实的承接引出）。
        - 达标即**销毁窗口**（Q16：一次 engaged 只落一条）：窗内余下消息不再计数，
          直到下一次承接重新开窗 —— 销毁比打 ``fired`` 标记更不容易写错。
        - 落库失败（开关关闭 / 存储异常）同样销毁窗口：本窗已用掉，不该反复重试。

        ❗ 入口自带过期判定（与 ``_prune_engaged`` 同条件）：tick 只是每用户每 30 秒
        扫一次，两次 tick 之间到达的消息若不判过期，会把 61 分钟前那段对话和现在
        这条接成一段（窗口语义是「一次连贯的对话」）。判定幂等，重复无害。
        """
        plain = str(text or "")
        window = self._engaged_windows.get(user_id)
        if window is not None and (now - window.opened).total_seconds() / 60 > _ENGAGED_WINDOW_MINUTES:
            self._engaged_windows.pop(user_id, None)
            window = None
        if window is None:
            if not catch:
                return False
            window = _EngagedWindow(opened=now)
            self._engaged_windows[user_id] = window
        window.replies += 1
        window.chars += len(plain)
        if plain:
            window.texts.append(plain)
        if window.replies < _ENGAGED_REPLY_MIN or window.chars < _ENGAGED_CHARS_MIN:
            return False
        desc = "\n".join(window.texts)
        self._engaged_windows.pop(user_id, None)
        return bool(append_milestone(self._plugin._engine.deps, user_id, desc, now))

    def _prune_engaged(self, user_id: str, now: datetime.datetime) -> None:
        """丢弃超时未达标的 engaged 窗（与冷落结算共用同一 tick 路径回收）。

        ⚠️ 必须由 ``settle_expired`` 在**最开头**调用：它有「无发送记录即早退」的
        分支，而 engaged 窗与发送记录互不引用 —— 放尾部会让「近期 bot 没主动开口、
        但用户自己找上门聊了一阵」的窗口永不回收（最多只泄漏一个 uid 的窗，
        但那是无界增长的开始，不能留）。
        """
        window = self._engaged_windows.get(user_id)
        if window is None:
            return
        if (now - window.opened).total_seconds() / 60 > _ENGAGED_WINDOW_MINUTES:
            self._engaged_windows.pop(user_id, None)

    def settle_expired(self, user_id: str, now: datetime.datetime) -> Tuple[int, int]:
        """窗口到期结算，返回 ``(冷落条数, 未送达条数)``。

        - **已送达且无人接住** → 计一次冷落（分享欲 -decay）；
        - **未送达** → 计一次 undelivered，**不参与冷落结算**：用户没看到，
          罚它只会让分享欲无谓下跌；
        - 已被承接的记录直接出队，不再计数。

        结算即出队，下一轮不会重复惩罚（等价于旧实现的"弹出即罚一次"）。

        本方法同时是 engaged 计数窗的**唯一回收点**（每用户每 tick 经过一次），
        回收调用放在首行以避开下方的早退分支。
        """
        self._prune_engaged(user_id, now)
        records = self._sent_records.get(user_id, [])
        if not records:
            return 0, 0
        keep: List[_SentRecord] = []
        ignored = 0
        undelivered = 0
        for rec in records:
            if (now - rec.ts).total_seconds() / 60 <= _PROACTIVE_CATCH_WINDOW_MINUTES:
                keep.append(rec)
                continue
            if rec.consumed:
                # 已按承接结算过（caught），不再参与任何到期结算——否则同一条
                # 开口会既算承接又算未送达，漏斗各层之和超过开口总数。
                continue
            if rec.delivered:
                ignored += 1
            else:
                undelivered += 1
        self._sent_records[user_id] = keep[-_SENT_KEEP_PER_USER:]
        return ignored, undelivered

    def clear_sent(self) -> None:
        """清空主动消息发送记录与 engaged 计数窗（状态重置用）。"""
        self._sent_records.clear()
        self._engaged_windows.clear()

    # ─── 内部 ────────────────────────────────────────────────────

    def _random_range(self) -> Tuple[int, int]:
        """随机间隔范围（分钟），非法时回退 60-240。"""
        cfg = self._plugin.config
        raw = list(cfg.proactive.random_minutes or [])
        values = [int(item) for item in raw if isinstance(item, (int, float)) and item > 0]
        if len(values) >= 2 and values[0] <= values[1]:
            return values[0], values[1]
        return 60, 240

    def _rules_for(self, user_id: str) -> List[Any]:
        """该用户的窗口规则（可能为空 → 走默认窗口）。"""
        cfg = self._plugin.config
        uid = str(user_id).strip()
        # getattr 兜底：热重载期间可能拿到尚未收敛到新字段的配置对象
        rules = getattr(cfg.proactive, "user_window_rules", None) or []
        return [
            rule
            for rule in rules
            if str(getattr(rule, "user_id", "") or "").strip() == uid
        ]

    def _allowed_now(self, user_id: str, now: datetime.datetime) -> bool:
        """当前时刻是否允许对该用户主动开口。

        - 有按用户规则 → **完全覆盖**默认窗口，任一规则命中即可；全不命中就绝不打扰
          （含"规则存在但 days 为空" = 永不主动）。
        - 无规则 → 退回 ``default_active_window``（无星期维度）。
        """
        rules = self._rules_for(user_id)
        if rules:
            return any(rule_matches_now(now, rule) for rule in rules)
        return in_windows(now.time(), self._plugin.config.proactive.default_active_window or [])


__all__ = [
    "ProactiveScheduler",
    "in_silent",
    "in_windows",
    "parse_window",
    "rule_matches_now",
    "validate_rules",
]