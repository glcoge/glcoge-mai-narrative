"""API 环节（深化 C2）：narrative_state / diary_context / chronicle_append 实现体。

从 plugin.py 下沉；plugin 侧壳只保留 @API 声明与一行转发。
「给 diary 的当日生活片段取数」契约（常量 + _same_day_chronicle）随体迁入。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .render.audience import drop_diary
from .state.engine import INJECT_TEXT_CAP
from .store import SOURCE_DIARY

# ===== 跨插件契约：给 diary 的「当日生活片段」取数（2026-10-01 接线） =====

#: 回给 diary 的当日**普通**生活片段条数上限（2/4/6 三选一 → 用户裁定 4）
_DIARY_FRAGMENT_LIMIT = 4
#: 普通片段每条的截断字数（用户裁定 120：够"有个印象"，又不让 prompt 膨胀；
#: 高光另算——条目少且要求写进正文，给全文，只受 INJECT_TEXT_CAP 兜底）
_DIARY_FRAGMENT_CAP = 120
#: 单 kind 的读取上限（store.list_chronicle_rows 内部还会夹到 2000）
_DIARY_KIND_FETCH_LIMIT = 200


def _same_day_chronicle(store: Any, kind: str, day: str) -> List[str]:
    """取 scope=self 某一天的某类编年史正文，**按时间升序**返回。

    为什么按 date 精取、而不是直接给"最近 N 条"（2026-10-01 设计裁决）：
    日记在次日 04:00 生成、写的是**昨天**——此时的"最近 N 条"恰好落在今天
    凌晨，会把昨天的片段全挤掉。口径必须跟着**被写的那天**走，故由调用方
    显式传 ``date``；空则由调用方兜底成插件时区的今天（即时重试仍然正确）。

    ⚠ **口径边界（已知、暂不修）**：diary 的收集窗口是 ``[date 04:00, date+1 04:00)``，
    这里按**自然日** ``[date 00:00, date 23:59]`` 取片段，两端各差 4 小时。
    实际影响可忽略——作息默认 23:30 睡 / 07:00 起，00:00-04:00 几乎不产片段；
    若日后关掉睡眠态或改了作息，先回来确认这条再决定是否改成传区间。

    读取策略刻意**绕过** ``visible_chronicle``：那条路径会按受众过滤，而
    用户 2026-10-01 裁定「涉私与否不区分，生活片段全部进日记」（日记是作者
    本人的私密产物，不外发他人）。但 ADR-0004「diary 产物完全隔离」是立项
    铁律、不随该裁定豁免 → 仍用 ``drop_diary`` 兜住 kind=diary /
    source_uid=diary 的条目。
    """
    target = str(day or "").strip()[:10]
    if not target:
        return []
    texts: List[str] = []
    rows = store.list_chronicle_rows("self", limit=_DIARY_KIND_FETCH_LIMIT, kind=kind)
    for entry in drop_diary(rows):
        if str(entry.get("ts") or "").strip()[:10] != target:
            continue
        body = str(entry.get("text") or "").strip()
        if body:
            texts.append(body)
    texts.reverse()  # store 返回新→旧；转**时间升序**再交给 diary，便于按流水写
    return texts


async def state_api(plugin, **kwargs: Any) -> Dict[str, Any]:
    """供调试/外部读取的状态摘要。"""
    del kwargs
    if plugin._engine is None or plugin._store is None:
        return {"ok": False, "error": "not_initialized"}
    state = plugin._engine.load_self_state()
    summary: Dict[str, Any] = {
        "narrative_enabled": plugin.config.narrative.enabled,
        "proactive_enabled": plugin.config.proactive.enabled,
        "mood": state["state"]["mood"]["label"],
        "energy": state["state"]["mood"]["energy"],
        "routine_phase": state["state"]["routine"]["phase"],
        "mode_user_ids": plugin._mode_user_ids(),
        "known_streams": plugin._streams.known_count(),
        "chronicle_count": plugin._store.count_chronicle("self"),
    }
    summary["branches"] = {
        uid: {
            "stage": str(
                plugin._engine.load_branch_state(uid)
                .get("relationship", {})
                .get("stage")
                or "陌生人"
            ),
            # OBSERVE(R6)：互动计数是**内部证据计数**，不进 API 输出
            # （ADR-0002 §2：不进注入块、不进 status）
        }
        for uid in plugin._mode_user_ids()
    }
    return {"ok": True, **summary}

# ===== API：日记插件握手（跨插件协作，public=True） =====

async def diary_context_api(
    plugin, date: str = "", **kwargs: Any
) -> Dict[str, Any]:
    """日记生成时的剧本模式分诊数据源。

    Args:
        date: **被写日记的日期**（``YYYY-MM-DD``）。diary 在次日 04:00 生成、
            写的是昨天，必须显式传对方那天；不传（旧版 diary）退化为本插件
            时区的今天 —— 04:00 补写昨天那篇会因此取到空，**降级不误取**。
    """
    del kwargs
    if plugin._engine is None or plugin._store is None:
        return {"ok": False, "available": False, "error": "not_initialized"}

    cfg = plugin.config
    narrative_on = bool(cfg.plugin.enabled and cfg.narrative.enabled)
    state = plugin._engine.load_self_state()
    inner = state["state"]
    identity = cfg.identity

    # 锚定层人设 → 作者人格描述：复用原生 [personality].personality（人设唯一来源）+
    # 插件世界观字段。与 render.build_context_block 同源原则：本 API 只回摘要。
    # 表达风格提示 expression_hint 同样从原生 reply_style 派生（人设唯一事实源，
    # 插件不再重复定义性格/语气——曾用 immutable_traits，与原生冲突已删除）。
    persona_parts: List[str] = []
    try:
        native_personality = str(
            await plugin.ctx.config.get("personality.personality", "") or ""
        ).strip()
    except Exception as exc:
        native_personality = ""
        plugin.ctx.logger.debug("读取原生 personality 失败: %s", exc)
    if native_personality:
        persona_parts.append(native_personality)
    if identity.world:
        persona_parts.append(f"生活在{identity.world}")
    try:
        native_reply_style = str(
            await plugin.ctx.config.get("personality.reply_style", "") or ""
        ).strip()
    except Exception as exc:
        native_reply_style = ""
        plugin.ctx.logger.debug("读取原生 reply_style 失败: %s", exc)
    expression_hint = native_reply_style

    mood = inner["mood"]
    # 当日生活素材（2026-10-01 接线）：此前 latest_life_fragment /
    # recent_chronicle 两个字段 diary 侧一个都没读 → 日记里只剩聊天话题。
    target_day = str(date or "").strip()[:10] or plugin._local_now().strftime("%Y-%m-%d")
    day_highlights = _same_day_chronicle(plugin._store, "life_highlight", target_day)
    day_fragments = _same_day_chronicle(plugin._store, "life", target_day)[
        -_DIARY_FRAGMENT_LIMIT:
    ]
    return {
        "ok": True,
        "available": True,
        "narrative_enabled": narrative_on,
        "mode_user_ids": plugin._mode_user_ids(),
        "mode_stream_ids": [str(item) for item in (cfg.narrative.mode_stream_ids or [])],
        "self_state": {
            "identity_persona": "，".join(persona_parts),
            "expression_hint": expression_hint,
            "mood_label": str(mood.get("label", "平静")),
            "mood_energy": float(mood.get("energy", 0.5)),
            "mood_shift_ts": str(mood.get("last_shift_ts", "")),
            "routine_phase": str(inner["routine"].get("phase", "")),
            # 当日生活素材（高光全部 + 普通片段最近 4 条，各自按时间升序）。
            # ⚠ 跨插件 lockstep：diary 必须**同版部署**才会传 date；旧 diary
            # 不传参数（或 narrative 侧尚无 life_highlight 时）取到空列表属
            # 预期降级，**不得**据此判定"生活片段又没了"。
            "today_highlights": [text[:INJECT_TEXT_CAP] for text in day_highlights],
            "today_life_fragments": [
                text[:_DIARY_FRAGMENT_CAP] for text in day_fragments
            ],
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
            # OBSERVE(R18)：口径维持现状（继续给原文，用户 Q1-b 裁定）。
            # 但「diary 产物完全隔离」是立项铁律，不随该裁定豁免 → 只短路 diary。
            "recent_chronicle": [
                str(entry.get("text", ""))[:INJECT_TEXT_CAP]
                for entry in drop_diary(plugin._store.recent_chronicle("self", limit=3))
                if str(entry.get("text", "")).strip()
            ],
        },
        # TODO(Round 3)：每日心情轨迹表尚未建（v0.1 只有当前快照）。
        # 情绪轨迹增强排期靠后，日记侧已预留注入点，先返回空列表。
        "today_mood_track": [],
    }

async def chronicle_append_api(
    plugin,
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

    cfg = plugin.config
    if not (cfg.plugin.enabled and cfg.narrative.enabled and cfg.narrative.chronicle_enabled):
        return {"ok": True, "written": False, "date": date, "reason": "chronicle_disabled"}
    if plugin._store is None:
        return {"ok": False, "written": False, "error": "not_initialized"}

    normalized_audience = str(audience or "").strip()
    written = plugin._store.append_chronicle_once(
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
