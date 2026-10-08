"""入站环节（深化 C1）：chat.receive.after_process hook 的实现体。

从 plugin.py 下沉（声明与实现分离）；plugin 侧壳只做声明与 **kwargs
整体透传（🔴 逐键不拆——漏键症状是落痕/注入静默失效；🔴 转发层不新增
try/except，异常吞噬面由装饰器声明与实现体承担）。
"""

from __future__ import annotations

from typing import Any, Dict

from .learning.pairs import message_id_of
from .learning.suggestion import record_suggestion
from .message import extract_group_id, extract_user_id, is_private_chat, looks_like_command, message_text


async def handle_inbound_message(plugin, **kwargs: Any) -> Dict[str, Any]:
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
    if plugin._engine is None or plugin._store is None or plugin._telemetry is None:
        plugin.ctx.logger.warning("narrative inbound: 服务未初始化，跳过")
        return {"action": "continue", "modified_kwargs": kwargs}
    if not isinstance(message, dict) or not message:
        plugin.ctx.logger.debug("narrative inbound: message 为空/非 dict（type=%s）", type(message).__name__)
        return {"action": "continue", "modified_kwargs": kwargs}

    user_id = extract_user_id(message)
    is_private = is_private_chat(message)
    is_mode = plugin._is_mode_uid(user_id)
    plugin.ctx.logger.debug(
        "narrative inbound: keys=%s | user_id=%r | stream_id=%r | is_private=%s | is_mode=%s | text=%s",
        sorted(message.keys()), user_id, stream_id, is_private, is_mode,
        message_text(message)[:30],
    )
    if not is_private:
        # R35：群聊观察（只读落库；恒 continue，**绝不 abort**——回复判定归宿主）
        observe_group(plugin, message, stream_id)
        return {"action": "continue", "modified_kwargs": kwargs}
    if not is_mode:
        plugin.ctx.logger.debug("narrative inbound: 用户不在模式名单，uid=%r", user_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    if stream_id:
        plugin._streams.record(user_id, stream_id)

    plain = message_text(message)
    # 命令/通知类消息不进剧本素材（命令是"你本人操作"，不是 bot 的生活）。
    # OBSERVE(R9)：宿主 is_command 字段不可靠，补本地正则兜底——命令被当成
    # 对话素材会污染创作层与关系值。宿主修好后删掉 looks_like_command 即可。
    is_command = bool(message.get("is_command")) or looks_like_command(plain)
    if is_command or bool(message.get("is_notify")):
        plugin.ctx.logger.debug("narrative inbound: 命令/通知消息（is_command=%s is_notify=%s），跳过素材采集 uid=%s",
                             message.get("is_command"), message.get("is_notify"), user_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    now = plugin._local_now()
    plugin._engine.record_interaction(user_id, plain, now)
    plugin._engine.record_branch_feedback(user_id, now)
    # 建议通道（批 3 / R42）：语义路由（规则先行，须挂靠 pending 事件才放行）。
    # 群聊已在上方提前 return（R35：内容不进生活线）——结构性排除。
    # 旁路纪律同 pairs：路由/写入失败绝不拖垮入站主链路。
    try:
        record_suggestion(plugin._engine.deps, user_id, plain, now)
    except Exception as exc:
        plugin.ctx.logger.warning("建议通道路由失败（不阻断）: %s", exc)
    # 验收采样（指标 1/2 的判定与登记下沉 Telemetry，2026-09-13 C5）
    plugin._telemetry.note_inbound(
        stream_id=stream_id, user_id=user_id, text=plain, now=now
    )
    # 验收指标 3：主动消息是否被接住（单指标 + 延迟分钟，2026-09-22 定案）
    #
    # 原先是 check_reply(30min) → elif check_late_reply(24h) 的判定链：一条用户
    # 消息只会落进其中一个分支，同时满足时 24h 的分子被吞掉，指标系统性低估
    # （A2 报告里的 24% 就是这么来的，真实值 78%）。改为单一入口 + 延迟连续量：
    # 有没有被接住用 count(*) 数，多快被接住用 avg(value) 看，口径在分析层切。
    latency = plugin._proactive.resolve_catch(user_id, now)
    if latency is not None:
        plugin._telemetry.record("proactive_replied", latency, user_id=user_id)
        # share_urge（v0.1.8）：被接住 → 正反馈（聊得起来，更想聊）
        plugin._engine.record_urge_feedback(user_id, "caught")
        # engaged 计数窗（方案 §6.1 / Q9=c）：承接命中 = 开窗，本条计入 replies=1。
        # 达标（窗内 ≥3 条且 ≥30 字）时由 scheduler 直接落一条里程碑。
        plugin._proactive.note_engaged(user_id, plain, now, catch=True)
    else:
        # 窗内的普通消息照常计入（命令/通知已在上方 return，天然继承该口径）
        plugin._proactive.note_engaged(user_id, plain, now)
        if plugin._telemetry.is_user_initiated(stream_id or user_id, now):
            # share_urge（v0.1.8）：用户主动发起（非回复主动消息）→ 被需要感
            # ❗ 必须留在 else 内：拆成并列 if 会让承接分支也触发本反馈（双抬分享欲）
            plugin._engine.record_urge_feedback(user_id, "user_initiated")
    # 互动配对（批 4-C2 / R31）：把本次入站登记为「待配对的用户反馈」，
    # 等本轮出站时与之配成（用户反馈 id → 已送达回应 id）。只建不消费。
    if plugin._pairs is not None:
        plugin._pairs.note_inbound(user_id, message_id_of(message), now)
    plugin.ctx.logger.debug("narrative inbound: 落痕完成 uid=%s stream=%s", user_id, stream_id)
    return {"action": "continue", "modified_kwargs": kwargs}

def observe_group(plugin, message: Dict[str, Any], stream_id: str) -> None:
    """R35：群聊消息落一条**纯观察**事件（本批唯一的群聊写库动作）。

    三条设计约束（2026-09-29 群聊 grill 裁定）：
    1. **恒 continue、绝不 abort** —— 群里该不该回完全交给宿主的
       ``reply_necessity``（Q9=A：意愿门不自建），本方法不参与任何回复判定；
    2. **fail-closed** —— gid 取不到 / 群不在观察名单 / 命令通知 一律拒绝落库；
    3. **不接任何私聊语义链路** —— 不写支线、不更新互动、不进 telemetry、
       不做承接结算、不做分享欲反馈、不建互动配对（Q2=B）。

    ⚠️ 步骤 0 侦察：上线初期请把 logger 调到 debug，确认第一条日志里的
    ``gid`` 非空——取不到群号说明宿主载荷结构与本实现的预期不符，
    此时**整个 R35 应停在这里**，去修 ``extract_group_id``；
    绝不可降级为「按私聊素材处理」，那正是 09-21 泄露事故的路径。
    """
    if not (plugin.config.plugin.enabled and plugin.config.narrative.enabled):
        return
    if plugin._engine is None or plugin._store is None:
        return
    group_id = extract_group_id(message)
    group_info = (message.get("message_info") or {}).get("group_info") if isinstance(message, dict) else None
    plugin.ctx.logger.debug(
        "narrative inbound(group): gid=%r | session=%r | group_info=%r | keys=%s | text=%s",
        group_id, stream_id, group_info,
        sorted(message.keys()) if isinstance(message, dict) else [],
        message_text(message)[:30],
    )
    # 侦察信号：每条群消息都打会刷屏，但**一次都不打**又等于没有侦察证据
    # → 每次进程只打第一条（够用来确认 group_info 结构，重启后再确认一次）。
    # ⚠️ 这是 R35 上线验收的第一步：**部署后必须在日志里看到 gid 非空**。
    if not plugin._group_recon_logged:
        plugin._group_recon_logged = True
        plugin.ctx.logger.info(
            "narrative 群聊侦察（首条）：gid=%r | session=%r | group_info=%r | keys=%s",
            group_id, stream_id, group_info,
            sorted(message.keys()) if isinstance(message, dict) else [],
        )
    if not group_id:
        # ❗ fail-closed：取不到群号 = 无法打受众标 = 会被当成通用素材进私聊，
        # 宁可不记。这是 09-21 泄露事故的同型路径，绝不放行。
        plugin.ctx.logger.warning(
            "narrative 群聊观察：取不到群号（group_info=%r），拒绝落库", group_info
        )
        return
    allowed = set(plugin._observed_group_ids())
    if not allowed:
        return  # 观察整体关闭（默认状态）：连名单都没有，不必再看消息内容
    if group_id not in allowed:
        plugin.ctx.logger.debug("narrative 群聊观察：群 %s 不在观察名单", group_id)
        return
    plain = message_text(message)
    # OBSERVE(R9) 同款兜底：命令/通知不是"她说的话"，不进语料
    if bool(message.get("is_command")) or bool(message.get("is_notify")) or looks_like_command(plain):
        return
    plugin._engine.record_group_material(group_id, plain, now=plugin._local_now())
    if stream_id and plugin._group_streams is not None:
        # replyer hook 载荷没有群字段 → 只能靠入站时把 session→gid 记下来
        plugin._group_streams.record(group_id, stream_id)
