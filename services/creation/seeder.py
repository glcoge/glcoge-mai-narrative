"""世界事件源（播种器）：独立题材供给的第二根管子（批 2 / R40 / 总览 §6.2 输入②）。

血统与界限（总览 §5.3 三栏）：
- **借** HDSI 的供给思想——周期低概率生成外部世界事件，语义为
  「事实权威、反应自由」（她只是目击/经历，不是事件的编剧）；
- **弃** HDSI 的约束机制——无剧本、无单写者锁、无权威时钟；
- 重要性（low/mid/high）× 时效（immediate/short/long）分类学**直抄** HDSI，
  本批只登记进事件实体元数据，消费端不接（「先数据后功能」，R29 同款纪律）。

🔴 **参与者禁入双层**（红线①，执行路线 §跨批约定）：
1. **生成端 prompt 明示**：``SEEDER_BAN_RULE`` 模板常量（测试钉死其存在）；
2. **落库前词面拦截**：``_blocklist()`` = 模式用户 + StreamRegistry 全量已知 uid
   + 群 gid + ``[seeder].blocked_names`` 手工网名；命中**丢弃整条** + WARNING
   （带原文，R30 同款「没有原文只能事后猜」教训）+ ``seed_blocked`` 计数。
   被拦截的不占每日上限（拦截≠播种）。

节奏四闸（全部走配置，无数据不写死常量）：enabled → interval → probability →
daily_max；kv 键与生活片段完全分域（``seed:last_try`` / ``seed:count:{date}``）。

落账双写：``make_seed_event``（kind=seed，与 fragment 同池同容量 LRU）+
编年史 ``kind=life_seed``（``GENERAL_KINDS`` 已扩容，晋升证据掩码自动放行）。
"""

from __future__ import annotations

import datetime
import json
import random
from typing import Any, Dict, List, Optional, Tuple

from ..kvkeys import SEED_COUNT as _SEED_COUNT_KEY
from ..kvkeys import SEED_LAST_TRY as _SEED_LAST_TRY_KEY
from .event_entity import make_seed_event
from .pipeline import commit_created_event, interval_gate, log_guard_reject

__all__ = [
    "SEEDER_BAN_RULE",
    "build_seed_prompt",
    "maybe_seed_world_event",
]

# OBSERVE(R40)：世界事件源播种器（登记表 R40 行）；抽样人工审（≥20 条零命中）
# 与取材率观察挂本模块的日志与 seed_blocked 计数。

#: 🔴 红线①第一层：生成端禁令段（模板常量——删掉即破防，测试钉死）
SEEDER_BAN_RULE = (
    "硬规则（不可违反）：这件正在发生的事不得涉及任何真实用户、网友或群友——"
    "不得出现任何 QQ 号、真实人名、用户的昵称或对用户的称呼，"
    "也不要写「某个网友/某个朋友」这类真实社交对象的具体行为。"
    "若提供了 NPC 名册，事件中出现的人物只能从名册里选；"
    "名册为空时只写环境、天气、城市生活或你自己的生活边缘，不要虚构具名人物。"
)

#: 拦截计数指标名（telemetry 通用通道；命名风格与 proactive_sent 一致）
SEED_BLOCKED_METRIC = "seed_blocked"

#: JSON 解析失败时的降级元数据（与 make_seed_event 缺省值同一来源）
_DEFAULT_IMPORTANCE = "low"
_DEFAULT_URGENCY = "short"
_VALID_IMPORTANCE = ("low", "mid", "high")
_VALID_URGENCY = ("immediate", "short", "long")

#: 播种 RNG：模块级实例，单测 monkeypatch ``seeder._rng`` 钉住（life._rng 同款手法）
_rng = random.Random()


def _seeder_config(deps: Any) -> Any:
    """取 [seeder] 段（缺段 = 关闭：老测试夹具兼容，build_guard_keywords 同款先例）。"""
    return getattr(deps.config, "seeder", None)


def _blocklist(deps: Any) -> List[str]:
    """参与者拦截词表（红线①第二层数据源）：模式用户 + 已知 uid/gid + 手工网名。"""
    names: List[str] = []
    seeder_cfg = _seeder_config(deps)
    if seeder_cfg is not None:
        names += [str(item) for item in (getattr(seeder_cfg, "blocked_names", []) or [])]
    narrative = getattr(deps.config, "narrative", None)
    if narrative is not None:
        names += [str(item) for item in (getattr(narrative, "mode_user_ids", []) or [])]
    streams = deps.streams
    known_uids = getattr(streams, "known_uids", None)
    if callable(known_uids):
        names += [str(item) for item in known_uids()]
    group_streams = deps.group_streams
    known_gids = getattr(group_streams, "known_gids", None)
    if callable(known_gids):
        names += [str(item) for item in known_gids()]
    return [name for name in (item.strip() for item in names) if name]


def _participant_hit(text: str, blocklist: List[str]) -> Optional[str]:
    """词面拦截判定：返回命中的词表项（未命中返回 None）。"""
    for name in blocklist:
        if name in text:
            return name
    return None


def build_seed_prompt(deps: Any, state: Dict[str, Any], now: datetime.datetime) -> str:
    """播种 prompt：世界基调 + NPC 抽样 + 她此刻的状态 + 禁令段 + 输出格式。

    纯函数便于测试；``_rng.sample`` 供单测钉住 NPC 抽样。
    """
    cfg = deps.config
    inner = state["state"]

    world_lines: List[str] = []
    cast_lines: List[str] = []
    lorebook = deps.lorebook
    if lorebook is not None:
        world_lines = [
            entry.content
            for entry in lorebook.entries()
            if entry.kind == "world" and entry.enabled
        ][:3]
        cast_pool = lorebook.list_cast()
        if cast_pool:
            cast_lines = [
                f"{entry.name}：{entry.content}"
                for entry in _rng.sample(cast_pool, min(2, len(cast_pool)))
            ]

    parts: List[str] = [
        "你要为「她的生活」构思一件此刻正在她的世界里发生的小事——"
        "她可能目击、听说或亲身路过，但不是她在网上与人发生的事。",
    ]
    if world_lines:
        parts.append("她的世界基调：\n" + "\n".join(f"- {line}" for line in world_lines))
    if cast_lines:
        parts.append("可登场配角（只能从中选）：\n" + "\n".join(f"- {line}" for line in cast_lines))
    parts.append(
        f"她此刻：{inner['routine']['phase']}，心情{inner['mood']['label']}，"
        f"精力 {float(inner['mood'].get('energy', 0.5)) * 10:.0f}/10，"
        f"时间 {now.strftime('%H:%M')}。"
    )
    parts.append(
        "任务：写一件具体的小事（正在发生或刚刚发生），30~60 字，有画面感；"
        "不要评论、不要抒情、不要对任何人喊话。"
    )
    parts.append(SEEDER_BAN_RULE)
    parts.append(
        '只输出 JSON：{"text": "事件一句话", "importance": "low|mid|high", '
        '"urgency": "immediate|short|long"}'
    )
    _ = cfg  # cfg 仅保留形参对称性（未来加 [seeder] 生成参数时不再改签名）
    return "\n\n".join(parts)


def _parse_seed_output(raw: str) -> Tuple[Dict[str, str], str, bool]:
    """解析生成输出：返回 (元数据, 事件文本, 是否降级)。

    JSON（含 ```json 围栏）解析成功且 text 非空 → 透传 importance/urgency；
    否则**降级**：原文整体作为事件文本 + 默认元数据（不丢弃产出，观察期宽容）。
    """
    text = str(raw or "").strip()
    payload = text
    if payload.startswith("```"):
        payload = payload.strip("`")
        if payload.lower().startswith("json"):
            payload = payload[4:]
        payload = payload.strip()
    try:
        data = json.loads(payload)
        event_text = str(data.get("text", "") or "").strip()
        if event_text:
            importance = str(data.get("importance", "") or "").strip()
            urgency = str(data.get("urgency", "") or "").strip()
            return (
                {
                    "importance": importance if importance in _VALID_IMPORTANCE else _DEFAULT_IMPORTANCE,
                    "urgency": urgency if urgency in _VALID_URGENCY else _DEFAULT_URGENCY,
                },
                event_text,
                False,
            )
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"importance": _DEFAULT_IMPORTANCE, "urgency": _DEFAULT_URGENCY}, text, True


async def maybe_seed_world_event(deps: Any, now: Optional[datetime.datetime] = None) -> None:
    """播种 tick：四闸 → 生成 → 拦截 → 落账（异常由 engine tick 的 try/except 兜住）。"""
    cfg = deps.config
    if not cfg.plugin.enabled or not cfg.narrative.enabled:
        return
    seeder_cfg = _seeder_config(deps)
    if seeder_cfg is None or not seeder_cfg.enabled:
        return

    current = now or deps.local_now()
    store = deps.store

    # ② interval 闸：距上次尝试未满间隔 → 本 tick 直接返回（基元语义与原内联一致）
    if not interval_gate(store, _SEED_LAST_TRY_KEY, current, minutes=max(1, int(seeder_cfg.interval_minutes))):
        return
    # 推进 last_try：本 interval 只尝试一次（概率失败/生成失败/被拦截都不重试）
    # ⚠️ 与 life 的「成功后推进」是**真语义差异**（深化 A 差异表），编排保留本地。
    store.set_kv_str(_SEED_LAST_TRY_KEY, current.isoformat(timespec="seconds"))

    # ③ 概率闸：低概率起步（HDSI 量级：期望每天 1~2 条，见 config 注释）
    if _rng.random() >= float(seeder_cfg.probability):
        return

    # ④ 每日上限（被拦截的不占配额——拦截 ≠ 播种）
    today = current.strftime("%Y-%m-%d")
    count_key = f"{_SEED_COUNT_KEY}{today}"
    if store.get_kv_int(count_key) >= max(1, int(seeder_cfg.daily_max)):
        return

    # 生成（LLM 一次调用）
    state = deps.state.load_self_state()
    prompt = build_seed_prompt(deps, state, current)
    raw = await deps.creator.generate(prompt)
    if not str(raw or "").strip():
        return
    meta, event_text, degraded = _parse_seed_output(raw)
    if not event_text:
        return
    if degraded:
        deps.logger.warning(
            "播种事件输出非约定 JSON → 降级为纯文本（默认 low/short）；原文: %s",
            event_text[:60],
        )

    # 🔴 红线①第二层：落库前词面拦截（命中丢弃整条，不入库不占配额）
    hit = _participant_hit(event_text, _blocklist(deps))
    if hit is not None:
        telemetry = deps.telemetry
        if telemetry is not None:
            telemetry.record(SEED_BLOCKED_METRIC, 1, scope="seeder")
        # 统一格式助手（深化 A）：ellipsis=False 保 seeder 现行裸截断文案。
        log_guard_reject(
            deps.logger,
            label=f"播种事件命中参与者拦截（命中={hit}）",
            text=event_text,
            ellipsis=False,
        )
        return

    # 落账双写：事件实体（kind=seed，与 fragment 同池同容量 LRU）+ 编年史 life_seed
    # ——尾段收口至 pipeline.commit_created_event（深化 A）。
    commit_created_event(
        deps,
        state,
        entry=make_seed_event(
            ts=current.isoformat(timespec="seconds"),
            text=event_text,
            importance=meta["importance"],
            urgency=meta["urgency"],
        ),
        chronicle_kind="life_seed",
        now=current,
    )

    store.set_kv_int(count_key, store.get_kv_int(count_key) + 1)
    deps.logger.info(
        "世界事件已播种: importance=%s urgency=%s text=%s",
        meta["importance"],
        meta["urgency"],
        event_text[:60],
    )
