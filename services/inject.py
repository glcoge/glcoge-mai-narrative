"""注入环节（深化 C1）：planner/replyer 注入、表达隔离、REPLY_EXTENSION 实现体。

五个 hook/扩展的实现体从 plugin.py 下沉；壳只做声明与整体透传
（🔴 **kwargs 逐键不拆；🔴 转发层不新增 try/except——reply 扩展的
全体自捕获是宿主契约（扩展异常 = 整次 reply 失败），随实现体保留）。
"""

from __future__ import annotations

from typing import Any, Dict, Sequence

import time

from .learning.drift_style import describe_drift

from .render.audience import visible_chronicle
from .render.planner_block import (
    build_context_block,
    build_injected_item,
    is_injected_item,
    items_dialogue_text,
)
from .render.replyer_block import build_replyer_block, build_style_item, is_style_item

# REPLY_EXTENSION 组件名（批 4 / R39）：full_name = f"{manifest_id}.{本名}"；
# 定义移本模块（深化 C1）：注入体日志与 plugin 装配/装饰器共用单一事实源。
_REPLY_EXTENSION_NAME = "proactive_bysource"

#: 漂移注入耗时预算（ms）。宿主 hook 硬超时 6 秒，此处是**内部告警门槛**（R-C）：
#: 超预算说明注入路径被拖慢，宿主不会报错（异常全吞），只能靠这条 WARN 预警。
_DRIFT_BUDGET_MS = 500


async def inject_life_context(plugin, **kwargs: Any) -> Dict[str, Any]:
    """把自我层/支线层/编年史渲染成 item 追加进请求。"""
    # A/B 对照 gate（2026-09-10）：narrative.enabled=false 时剧本行为全停，
    # 但入站/出站采样 hook 无本 gate 照常采集——对照组数据口径的关键。
    if not (plugin.config.plugin.enabled and plugin.config.narrative.enabled):
        return {"action": "continue", "modified_kwargs": kwargs}
    session_id = str(kwargs.get("session_id") or "")
    items = kwargs.get("items")
    if plugin._engine is None or plugin._store is None:
        return {"action": "continue", "modified_kwargs": kwargs}
    if not plugin._is_mode_session(session_id):
        return {"action": "continue", "modified_kwargs": kwargs}
    if not isinstance(items, list) or not items:
        return {"action": "continue", "modified_kwargs": kwargs}
    if any(is_injected_item(item) for item in items):
        return {"action": "continue", "modified_kwargs": kwargs}

    user_id = plugin._streams.uid_of(session_id)
    state = plugin._engine.load_self_state()
    branch = plugin._engine.load_branch_state(user_id) if user_id else None
    # 受众过滤（ADR-0004 第 2 层）：涉私素材只讲给本人，diary 产物对所有人短路
    recent = visible_chronicle(plugin._store, "self", user_id, 3)
    round_kind, bysource = plugin._proactive.consume_pending(session_id)
    # 世界书触发扫描输入（批 1 / R43）：最近几轮对话文本，零 LLM token；
    # loader 未建（enabled=false）时跳过提取，省一次 items 遍历。
    dialogue_text = items_dialogue_text(items) if plugin._lorebook is not None else ""
    context_text = build_context_block(
        plugin,
        state,
        branch,
        plugin._local_now(),
        recent,
        round_kind=round_kind,
        bysource=bysource,
        audience=user_id,
        dialogue_text=dialogue_text,
    )
    items.append(build_injected_item(context_text))
    kwargs["items"] = items
    # 注入追踪（含日照锚点/心情/精力段，debug 级不刷盘时需临时调高日志级别）
    plugin.ctx.logger.debug(
        "narrative 注入: stream=%s | %s",
        session_id or "-",
        context_text[:100].replace("\n", " "),
    )
    return {"action": "continue", "modified_kwargs": kwargs}

async def inject_drift_style(plugin, **kwargs: Any) -> Dict[str, Any]:
    """把此刻状态事实段追加进 replyer 请求 items。

    ⚠️ 三条硬约束（ADR-0003 §5 + 宿主源码实测 H2/H3/H7）：
    1. 宿主 ``modified_kwargs`` 是**整体替换非合并** → 必须全量带出 kwargs，只改 items；
    2. hook 有 **6 秒硬超时** → 本路径只读一次 state，不读编年史，零 LLM；
    3. 宿主 try/except **吞掉一切异常** → 注入失败完全静默，故埋耗时与注入计数。

    与 planner 注入块分工（E9）：本块只出此刻状态事实（2026-10-14 裁定：
    不注入风格类措辞），**不重复**生活内容。
    """
    # A/B 对照 gate：与 planner 注入同款双开关，narrative.enabled=false 时行为全停
    if not (plugin.config.plugin.enabled and plugin.config.narrative.enabled):
        return {"action": "continue", "modified_kwargs": kwargs}
    session_id = str(kwargs.get("session_id") or "")
    items = kwargs.get("items")
    if plugin._engine is None or plugin._store is None:
        return {"action": "continue", "modified_kwargs": kwargs}
    # 会话过滤：``session_id`` 实测原样等于 stream_id（H8）→ 直接复用判定。
    # 群聊（R35）：群会话**不是** mode session，但观察名单内的群同样要注入
    # 漂移层（Q6=A：漂移层调制不属"多层表达融合"，群聊照常）。
    group_id = plugin._group_streams.gid_of(session_id) if plugin._group_streams is not None else ""
    if group_id and group_id not in set(plugin._observed_group_ids()):
        group_id = ""  # 已从名单摘除 / 不再观察 → 按非群聊会话处理
    if not plugin._is_mode_session(session_id) and not group_id:
        return {"action": "continue", "modified_kwargs": kwargs}
    if not isinstance(items, list) or not items:
        return {"action": "continue", "modified_kwargs": kwargs}
    # 幂等：同轮若已有本块（多 handler / 重入）不再追加
    if any(is_style_item(item) for item in items):
        return {"action": "continue", "modified_kwargs": kwargs}

    started = time.perf_counter()
    state = plugin._engine.load_self_state()
    # 漂移注入（W2：2026-10-13 裁定停用 learned_style；2026-10-14 裁定调制段→纯事实段）。
    # 群/私差异已归零（learned_style 强制空），保留会话门（mode 会话 + 观察群）。
    if group_id:
        # 🔴 群聊与私聊的差异收敛为一处：learned_style **强制为空** ——
        #    它是 config.toml [learned] 区块里私聊学到的表达习惯（ADR-0002），
        #    投进群 = 跨流泄露（旧 ①② 关系可见性差异已随关系语境行摘除消失）。
        learned_style: Sequence[str] = ()
    else:
        learned_style = ()  # W2：停用注入，但保留投影机制（commands.py:209 自带 import）
    drift_text = describe_drift(state.get("state") or {})
    # 2026-10-14 裁定：漂移层只注入纯事实（心情/精力/睡眠态）；无事实可注入时
    # 跳过——不追加空 style item（空 user 消息进 prompt 是污染）。
    if not drift_text.strip():
        return {"action": "continue", "modified_kwargs": kwargs}
    context_text = build_replyer_block(
        drift_text=drift_text,
        learned_style=learned_style,
    )
    items.append(build_style_item(context_text))
    kwargs["items"] = items
    plugin._style_inject_count += 1

    elapsed_ms = (time.perf_counter() - started) * 1000
    if elapsed_ms > _DRIFT_BUDGET_MS:
        plugin.ctx.logger.warning(
            "narrative 漂移注入耗时 %.0fms 超预算(%dms)：宿主硬超时 6s，超时即静默回退",
            elapsed_ms,
            _DRIFT_BUDGET_MS,
        )
    plugin.ctx.logger.debug(
        "narrative 漂移注入: stream=%s 耗时=%.0fms | %s",
        session_id or "-",
        elapsed_ms,
        context_text[:100].replace("\n", " "),
    )
    return {"action": "continue", "modified_kwargs": kwargs}

async def block_expression_select(plugin, **kwargs: Any) -> Dict[str, Any]:
    """剧本模式会话直接 abort，让表达选择整体跳过。"""
    # 隔离是"剧本模式"专属行为：任一开关关闭时放行（continue，不是 abort）。
    # 2026-09-16 前无此判断，插件/剧本关闭期间仍会 abort 表达选择。
    cfg = plugin.config
    if not (cfg.plugin.enabled and cfg.narrative.enabled):
        return {"action": "continue", "modified_kwargs": kwargs}
    session_id = str(kwargs.get("session_id") or "")
    if plugin._is_mode_session(session_id):
        return {"action": "abort", "modified_kwargs": kwargs}
    return {"action": "continue", "modified_kwargs": kwargs}

async def block_expression_upsert(plugin, **kwargs: Any) -> Dict[str, Any]:
    """剧本模式会话 abort 单条写入。"""
    # 同 block_expression_select：任一开关关闭即放行，交还给主程序处理
    cfg = plugin.config
    if not (cfg.plugin.enabled and cfg.narrative.enabled):
        return {"action": "continue", "modified_kwargs": kwargs}
    session_id = str(kwargs.get("session_id") or "")
    if plugin._is_mode_session(session_id):
        return {"action": "abort", "modified_kwargs": kwargs}
    return {"action": "continue", "modified_kwargs": kwargs}

async def reply_ext_proactive_bysource(plugin, **payload: Any) -> Dict[str, Any]:
    """reply 扩展：prepare 返回由头承接提醒；其余 phase / 无由头 / 未启用一律空操作。"""
    try:
        phase = str(payload.get("phase", "") or "")
        if phase != "prepare":
            return {}  # before_send 本批不使用；未知 phase 默认空操作
        if not bool(getattr(plugin.config.proactive, "reply_extension_enabled", False)):
            return {}
        if plugin._proactive is None:
            return {}
        session_id = str(payload.get("session_id", "") or "")
        bysource = plugin._proactive.bysource_for_reply(session_id, plugin._local_now())
        if not bysource:
            return {}
        return {
            "extra_prompt": (
                f"本次是你主动开口的回合。你想说起的是（由头）：{bysource}\n"
                "请让回复自然地从这个由头出发——承接它、就着它说，"
                "但不要逐字复述，也不要解释这是主动消息。\n"
                # 转述口径（2026-10-16 终版）：与主动轮素材说明同句（reply_reference 同口径裁定）
                "素材里的叙述句只是你的经历背景，开口时用一两句打字聊天的口吻转述，"
                "别照搬原文的书面句式。"
            )
        }
    except Exception as exc:
        # 🔴 宿主语义：扩展异常即整次 reply 失败——宁可这轮没有由头提醒，不拖垮回复
        plugin.ctx.logger.error(
            "回复扩展 %s 异常（降级空操作）: %s", _REPLY_EXTENSION_NAME, exc, exc_info=True
        )
        return {}
