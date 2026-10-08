"""命令环节（深化 C2）：/narrative 命令实现体。

从 plugin.py 下沉；plugin 侧壳只保留 @Command 声明与一行转发。
分发器经 ``plugin._cmd_*`` / ``plugin._is_admin`` 实例属性调用——
测试 fake 以实例属性覆写这条路径（test_promotion_rollback）。
"""

from __future__ import annotations

from typing import Any, Dict, List

from .kvkeys import PROACTIVE_COUNT as _PROACTIVE_COUNT_KEY
from .learning.projection import get_style_projection
from .render.audience import filter_entries, visible_chronicle
from .state.continuity import SLOW_FIELD_PATHS, current_relationship_stage

# status 中可用列表的展示上限（超出截断，避免刷屏）
_AVAILABLE_SHOW_LIMIT = 10


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


async def handle(plugin, **kwargs: Any) -> Tuple[bool, str, bool]:
    """管理命令：help / status / reset。"""
    matched = (kwargs.get("matched_groups") or {}).get("sub") or ""
    stream_id = str(kwargs.get("stream_id", "") or "")
    user_id = str(kwargs.get("user_id", "") or "")
    if not plugin._is_admin(user_id):
        admin_list = list(plugin.config.plugin.admin_qq or [])
        msg = "⚠️ 未配置管理员" if not admin_list else "⚠️ 仅管理员可用"
        await plugin.ctx.send.text(msg, stream_id)
        return False, "no admin", True

    raw = str(matched or "").strip()
    if not raw or raw == "help":
        await plugin._cmd_help(stream_id)
        return True, "ok", True
    command, _, param = raw.partition(" ")
    if command == "status":
        await plugin._cmd_status(stream_id, user_id)
        return True, "ok", True
    if command == "reset":
        await plugin._cmd_reset(param, stream_id)
        return True, "done", True
    if command == "rollback":
        await plugin._cmd_rollback(param, stream_id)
        return True, "done", True
    await plugin.ctx.send.text(f"未知子命令: {command}。/narrative help 查看用法", stream_id)
    return False, "unknown sub", True

def is_admin(plugin, user_id: str) -> bool:
    """管理员白名单校验。"""
    admin_list = [str(item) for item in (plugin.config.plugin.admin_qq or [])]
    if not admin_list:
        return False
    return user_id in set(admin_list)

async def cmd_help(plugin, stream_id: str) -> None:
    text = (
        "/narrative help            - 查看本帮助\n"
        "/narrative status          - 剧本状态摘要（模式/心情/关系/编年史）\n"
        "/narrative reset           - 重置状态与事件（先输入 'reset' 显示确认）\n"
        "/narrative rollback [路径] - 撤销最近一次慢变晋升（last=最近一条；\n"
        "                             路径如 perspective.world_view）\n"
        "说明：配置在 WebUI 插件页修改（[identity] 锚定层人设请手动填写）。"
    )
    await plugin.ctx.send.text(text, stream_id)

async def cmd_rollback(plugin, param: str, stream_id: str) -> None:
    """撤销最近一次慢变晋升（批 4-C8）。

    ``/narrative rollback`` 或 ``... last`` → 全表最近一条 applied；
    ``/narrative rollback <path>`` → 该路径最近一条。
    """
    if plugin._promotion_engine is None or plugin._store is None:
        await plugin.ctx.send.text("⚠️ 晋升机未启用（[promotion].enabled=false）", stream_id)
        return
    requested = str(param or "").strip()
    if requested in ("", "last"):
        path = ""
    elif requested in SLOW_FIELD_PATHS:
        path = requested
    else:
        valid = "、".join(sorted(SLOW_FIELD_PATHS))
        await plugin.ctx.send.text(
            f"⚠️ 路径不在慢变白名单：{requested}\n可选：{valid}（或 last）", stream_id
        )
        return

    result = plugin._promotion_engine.rollback_last(path=path, now=plugin._local_now())
    status = str(result.get("status") or "")
    if status == "nothing":
        await plugin.ctx.send.text(
            "没有可撤销的晋升记录"
            + (f"（路径 {path}）" if path else "")
            + "。",
            stream_id,
        )
        return
    if status == "missing_scope":
        await plugin.ctx.send.text(
            f"⚠️ 该晋升是关系维度（{result.get('path')}）但审计缺少归属用户，"
            "拒绝瞎猜——请手工核对后处理。",
            stream_id,
        )
        return

    target = str(result.get("path") or "")
    await plugin.ctx.send.text(
        f"↩️ 已撤销最近一次晋升：{target}\n"
        f"恢复旧值：{result.get('restored')!r}\n"
        f"（审计 promotion_id={result.get('promotion_id')}）",
        stream_id,
    )
    # 投影跟着事实源走（只 general 维度才需要动 config.toml）
    if target.startswith("perspective."):
        plugin._rebuild_learned()

async def cmd_status(plugin, stream_id: str, audience: str = "") -> None:
    """状态摘要（不含聊天正文）。

    status 是对外面（发给命令发起者），同样按受众过滤（D6）：
    涉私素材只显示给本人，diary 产物一律不显示。
    """
    if plugin._engine is None or plugin._store is None:
        await plugin.ctx.send.text("插件尚未初始化完成，请稍后再试", stream_id)
        return
    cfg = plugin.config
    state = plugin._engine.load_self_state()
    inner = state["state"]
    # R10 退役（v0.2.0 批 1）：[creator_model] 直连已删，只剩按名路由一条路
    if str(cfg.llm.creation_model or "").strip():
        creator_line = f"创作模型: 按名路由（{cfg.llm.creation_model}）"
    else:
        creator_line = "创作模型: 默认（未配置，可填 [llm].creation_model）"
    mode_uids = plugin._mode_user_ids()
    lines = [
        "【剧本人设系统 · 状态】",
        f"剧本模式: {'开' if cfg.narrative.enabled else '关'} | "
        f"主动消息: {'开' if cfg.proactive.enabled else '关'}",
        creator_line,
        # N2 脱敏（D10/Q5）：状态文本曾把 mode_user_ids 原文外发给非管理员会话，
        # 真实事故——2026-09-22 14:12 发给 927386371 的状态含他人 QQ 号。
        # 只显示计数，不显示号码。
        f"模式用户: {len(mode_uids)} 人 | 已知会话: {plugin._streams.known_count()}",
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
        branch = plugin._engine.load_branch_state(uid)
        relationship = branch.get("relationship", {})
        stage = str(
            relationship.get("stage")
            or current_relationship_stage(relationship)
        )
        lines.append(f"支线[{index}]: {stage}")
    recent = visible_chronicle(plugin._store, "self", audience, 2)
    if recent:
        lines.append("编年史最近: " + str(recent[0].get("text", ""))[:40])
    today = plugin._local_now().strftime("%Y-%m-%d")
    for index, uid in enumerate(mode_uids, start=1):
        count = plugin._store.get_kv_int(f"proactive:count:{uid}:{today}")
        lines.append(f"今日主动[{index}]: {count}")
    # 学习层投影（批 3-C2 / R1）：[learned] 不在 config.py，只能从文件直读
    learned_style = get_style_projection(plugin._config_path(), logger=plugin.ctx.logger)
    if learned_style:
        lines.append(f"学习投影(R1): {len(learned_style)} 条")
    else:
        lines.append("学习投影(R1): 空（待批 4 晋升机写入）")
    # 漂移注入计数（E8）：宿主 hook 异常全吞，只有这里能证明「注入到底有没有上线」
    lines.append(f"漂移注入: 累计 {plugin._style_inject_count} 次")
    # 世界书状态（批 1 / R43）：换人设/填设定集时用这条核对 loader 是否真的读到了
    # getattr 缺段兜底与 planner_block 同款（旧测试夹具的 config 不带 lorebook 段）
    lorebook_cfg = getattr(plugin.config, "lorebook", None)
    if plugin._lorebook is not None and lorebook_cfg is not None:
        book = plugin._lorebook.entries()
        constants = sum(1 for entry in book if entry.constant)
        lines.append(
            f"世界书: {lorebook_cfg.mode} | 条目 {len(book)}（常驻 {constants}）| "
            f"NPC 名册 {len(plugin._lorebook.list_cast())} | 预算 {lorebook_cfg.inject_budget_chars} 字"
        )
    else:
        lines.append("世界书: 关")
    lines.append(f"数据目录: {plugin.ctx.paths.data_dir / 'narrative'}")
    # 宿主只开放「任务名」列表（非模型名），仅作连通性参考；失败不影响 status
    try:
        available = await plugin.ctx.llm.get_available_models()
        line = format_available_tasks_line([str(item) for item in (available or [])])
        if line:
            lines.append(line)
    except Exception as exc:
        plugin.ctx.logger.debug("获取可用任务列表失败: %s", exc)
    await plugin.ctx.send.text("\n".join(lines), stream_id)

async def cmd_reset(plugin, param: str, stream_id: str) -> None:
    """重置剧本状态：需显式确认 'yes' 或 'y'。"""
    normalized = str(param or "").strip().lower()
    if normalized not in ("yes", "y"):
        await plugin.ctx.send.text(
            "⚠️ 重置会清空自我层/支线层状态、事件队列与今日计数。"
            "确认请执行：/narrative reset yes",
            stream_id,
        )
        return
    deleted = plugin._store.delete_keys_with_prefix("")
    # 事件队列一并清空；编年史 append-only 刻意保留
    plugin._store.clear_all_events()
    plugin._streams.clear()
    if plugin._group_streams is not None:
        plugin._group_streams.clear()
    plugin._telemetry.clear_pending_rounds()
    plugin._proactive.clear_sent()
    # 全清 kv 会连 state 一起抹掉 → 立即重建默认 state（带当前 schema_version）。
    # 否则下次开库发现「没有 state」会当作首次初始化——语义上没错，但若日后
    # schema_version 改存 kv 键，就会被误判成「旧库」反复重置（批 2 F2 的坑）。
    plugin._engine.load_self_state()
    await plugin.ctx.send.text(
        f"已重置叙事状态（kv {deleted} 项、事件队列已清空；编年史保留未动）。", stream_id
    )

# ===== API：状态查询（仅元信息，不含正文） =====
