"""剧本人设系统插件配置模型（PluginConfigBase 段式定义）。

设计共识（grill-me 会话）：
- 锚定层（identity）由用户手动配置，运行时只读 —— 对应设计树 R4.1"锚定层"。
- 创作模型按**已注册模型名**路由（`[llm].creation_model`）；`[creator_model]` 直连已于 v0.2.0 批 1 退役（R10：宿主 1.2.5 已透传 model_name）
  直连 —— 对应 R5。注：宿主 `ctx.llm.generate(model=...)` 现在只认模型名，
  不认 task 名（2026-09-15 真机证实）。
- v0.1 最小切片：单人单私聊"剧本模式"。
"""

from __future__ import annotations

from typing import ClassVar, List, Literal

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础设置。"""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description=(
            "是否启用插件。关闭后停止：对话注入、主动消息、创作层、世界时钟 tick、"
            "表达学习隔离、支线关系值推进。⚠️ 这是插件自设的软开关，false 时插件仍会"
            "加载、hook 仍会触发，只是各入口自查后跳过；要彻底不加载请在 WebUI 禁用插件。"
        ),
        json_schema_extra={"label": "启用插件", "order": 1},
    )
    config_version: str = Field(
        default="0.1.3",
        description="配置文件版本号，由 SDK 自动维护。",
        json_schema_extra={"label": "配置版本", "disabled": True, "order": 2},
    )
    admin_qq: List[str] = Field(
        default_factory=list,
        description="/narrative 系列命令的 QQ 白名单。空列表 = 禁用所有命令。",
        json_schema_extra={
            "label": "管理员 QQ",
            "hint": '纯数字 QQ 号，例 ["123456"]',
            "item_type": "string",
            "placeholder": '["123456"]',
            "order": 3,
        },
    )


class IdentitySection(PluginConfigBase):
    """锚定层 —— 用户手动配置，运行时只读，任何代码不得改写。

    注意：**人设主体（身份白描/行为准则/说话风格）复用主程序原生配置**
    ``config/bot_config.toml`` 的 ``[personality]``（personality / behavior_style /
    reply_style）与 ``[bot].nickname``——系统提示里只保留这一份"你是谁"，
    避免双人格并置。本段只承载原生三段没有的维度：世界观 / 价值观底线 /
    世界观规则 / 不可变人格特征。
    """

    __ui_label__: ClassVar[str] = "锚定层（世界观与铁律）"
    __ui_icon__: ClassVar[str] = "user"
    __ui_order__: ClassVar[int] = 1

    world: str = Field(
        default="",
        description="世界观归属。例：塞博朋克沿海城市、普通现代都市。",
        json_schema_extra={"label": "世界观", "hint": "一句话世界观", "order": 1},
    )
    values: List[str] = Field(
        default_factory=list,
        description="价值观底线：注入对话时遵守。⚠️ 渲染时只取前 5 条，超出部分不注入。",
        json_schema_extra={
            "label": "价值观底线",
            "hint": '例 ["不撒谎","尊重每个玩家"]；注入时只取前 5 条',
            "item_type": "string",
            "order": 2,
        },
    )
    world_rules: List[str] = Field(
        default_factory=list,
        description="世界观规则/禁忌：世界内不可打破的设定。⚠️ 渲染时只取前 5 条。",
        json_schema_extra={
            "label": "世界观规则",
            "hint": '例 ["这个世界没有魔法","本市没有第 13 区"]；注入时只取前 5 条',
            "item_type": "string",
            "order": 3,
        },
    )
    guard_fragments: List[str] = Field(
        default_factory=list,
        description=(
            "禁用片段黑名单：世界/价值观/规则的**浓缩片段**（人物代号、专属称呼、"
            "指认性表述等）。产出文本或注入文本命中即拦下——世界里不该出现的东西"
            "（ADR-0002 §9）。与 [anchor].guard_keywords（字段名级手工补充）互补："
            "本项面向「内容片段」，那项面向「字段/代号名」。"
        ),
        json_schema_extra={
            "label": "禁用片段",
            "hint": '例 ["现实中的真实姓名","穿越前的身份"]；不填 = 只从 world_rules/values 自动抽取',
            "item_type": "string",
            "order": 4,
        },
    )


class AnchorSection(PluginConfigBase):
    """锚定层护栏（ADR-0002 §9，批 2）。

    锚定层内容本身在宿主 ``[personality]`` 与 ``[identity]`` 里；本段只管
    **护栏行为**：启动期一致性比对、双向守卫的手工关键词。
    """

    __ui_label__: ClassVar[str] = "锚定护栏"
    __ui_icon__: ClassVar[str] = "shield"
    __ui_order__: ClassVar[int] = 3

    consistency_check: bool = Field(
        default=False,
        description=(
            "启动期「宿主 [personality] vs 插件 world/values」LLM 一次性一致性比对，"
            "不一致仅 WARN 不阻断（真机出现过「大二女大学生 vs 隐姓埋名神兽」双人格混写）。"
            "⚠️ 默认关：启动期同步 LLM 调用一旦慢或失败会拖垮启动。"
            "默认关 = 通道建好、默认不跑。"
        ),
        json_schema_extra={"label": "启动期锚定一致性比对", "order": 1},
    )
    guard_keywords: List[str] = Field(
        default_factory=list,
        description=(
            "双向守卫（入库前 + 注入前）的**手工补充**关键词。"
            "自动部分另从 [identity].world_rules / values 抽取，无需在此重复。"
        ),
        json_schema_extra={
            "label": "守卫关键词（手工补充）",
            "hint": '例 ["内部代号","真实姓名"]；自动部分已含 world_rules/values',
            "item_type": "string",
            "order": 2,
        },
    )


class NarrativeSection(PluginConfigBase):
    """剧本模式与世界引擎设置。"""

    __ui_label__: ClassVar[str] = "剧本"
    __ui_icon__: ClassVar[str] = "book-open"
    __ui_order__: ClassVar[int] = 2

    enabled: bool = Field(
        default=False,
        description=(
            "剧本模式总开关。关闭后停止：对话注入、主动消息、创作层、世界时钟 tick、"
            "表达学习隔离、支线关系值推进。⚠️ 遥测采样仍继续（A/B 对照需要），"
            "命令仍可用。"
        ),
        json_schema_extra={"label": "剧本模式", "order": 1},
    )
    mode_user_ids: List[str] = Field(
        default_factory=list,
        description="剧本模式私聊用户 QQ 列表（v0.1 单人验证：只填你自己）。",
        json_schema_extra={
            "label": "剧本模式用户",
            "hint": "纯数字 QQ，例 [\"10001\"]；私聊自动识别",
            "item_type": "string",
            "placeholder": '["10001"]',
            "order": 2,
        },
    )
    mode_stream_ids: List[str] = Field(
        default_factory=list,
        description="剧本模式会话 stream_id 列表（可选：显式指定，一般不填）。",
        json_schema_extra={
            "label": "剧本模式会话",
            "hint": "高级用法；一般留空，插件自动学习",
            "item_type": "string",
            "order": 3,
        },
    )
    clock_tick_minutes: int = Field(
        default=30,
        ge=5,
        le=240,
        description="世界时钟 tick 间隔（分钟）。规则驱动，默认免 LLM。",
        json_schema_extra={
            "label": "时钟 tick 间隔",
            # 代码里有硬下限：engine.py 用 max(30, ...) 兜底，填 5~29 会静默按 30 跑
            "hint": "分钟；有效 30-240（小于 30 会按 30 计）",
            "order": 4,
        },
    )
    timezone_offset_hours: int = Field(
        default=8,
        ge=-12,
        le=14,
        description=(
            "剧本时区偏移（小时，UTC+）。影响作息阶段/注入时间/主动窗口/编年史日期。"
            "⚠️ 仅当服务器系统时区既非 UTC、又与剧本时区不一致时才真正生效；"
            "服务器为 UTC 时本项**无效**（引擎直接信墙钟），请直接改服务器系统时区。"
        ),
        json_schema_extra={
            "label": "时区偏移",
            "hint": "例：UTC+8=8、UTC-5=-5；⚠️ 服务器为 UTC 时不生效",
            "order": 5,
        },
    )
    # 2026-09-16 移除 event_max_daily / event_max_per_user_daily：
    # 全仓从未读取过（push_event 无限入队），文案却承诺"防止事件刷屏"。
    # 事件队列实际靠 _dequeue_expired_branch_events 的 3 天过期兜底。
    chronicle_enabled: bool = Field(
        default=True,
        description=(
            "编年史总开关（append-only 散文日记，供对话注入与日记插件握手）。"
            "关闭后三种写入全部停止：生活片段(life)、每日小结(daily)、日记握手(diary)。"
            "生活片段本身仍会照常生成（它是主动消息的由头来源）。"
        ),
        json_schema_extra={"label": "编年史", "order": 7},
    )
    # ── 精力规则参数（v0.1.5：修复无互动日 mood 贴地卡死，A/B 可调） ──
    energy_baseline: float = Field(
        default=0.45,
        ge=0.05,
        le=1.0,
        description="精力基线：无互动时精力每 tick 向该值回归（双向）。",
        json_schema_extra={"label": "精力基线", "hint": "0.05-1.0", "order": 8},
    )
    energy_baseline_pull: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "基线回归系数：每 tick 向基线靠拢的比例，越大回摆越快。"
            "0 = 关闭基线回归（精力不再向基线回落，只升不降）。"
            "⚠️ 不是「老版单调衰减」—— 旧版已废弃，0 现在是冻结语义。"
        ),
        json_schema_extra={"label": "基线回归系数", "hint": "0-1；0=冻结精力，默认 0.3", "order": 9},
    )
    energy_sleep_recovery: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description=(
            "睡眠中每 tick 额外恢复量：睡觉回血，早晨自然满状态。"
            "平衡点 = 基线 + 恢复量/回归系数（默认 0.45+0.1/0.3≈0.78，落在轻快档下沿）。"
            "（2026-09-22：判定由「深夜相位」改为「睡眠态」——旧写法只在 23:00-05:00 回血，"
            "05:00 到起床之间睡着却不回血，属漏网；且 23:00-23:30 明明还醒着反而回血。）"
        ),
        json_schema_extra={"label": "睡眠恢复", "hint": "每 tick 恢复量；默认 0.1", "order": 10},
    )
    energy_interaction_boost: float = Field(
        default=0.12,
        ge=0.0,
        le=1.0,
        description="近 2 小时内有互动时每 tick 额外提振量。",
        json_schema_extra={"label": "互动提振", "hint": "每 tick 提振量；默认 0.12", "order": 11},
    )
    daily_chronicle_time: str = Field(
        default="23:30",
        description=(
            "每日编年史压缩触发时间（HH:MM）。目标日期 = 触发点所属那天，"
            "当日有互动时用轻量模型写一条「今日小结」(kind=daily)。"
            "⚠️ 触发后由世界时钟 tick 驱动，最长滞后一个 tick 间隔；"
            "若 tick 错过该时间点，次日首次 tick 会补写昨天。"
        ),
        json_schema_extra={
            "label": "编年史压缩时间",
            "hint": "HH:MM；留空=不自动压缩；受 [narrative].chronicle_enabled 约束",
            "order": 8,
        },
    )
    life_fragment_interval_minutes: int = Field(
        default=60,
        ge=30,
        le=1440,
        description=(
            "生活片段生成的间隔（分钟）。创作层每隔 N 分钟可能用轻量模型生成一段"
            "「生活片段」；间隔越大越省 token。"
            "⚠️ 实际检查粒度 = 世界时钟 tick（clock_tick_minutes），"
            "实际间隔会被向上取整到 tick 的整数倍（tick=30 时填 45 等同 60）。"
            "⚠️ 间隔与日上限必须配套看：睡眠态启用后清醒窗口约 16.5 小时，"
            "间隔 120 时理论产能只有 8 条，日上限填再大也吃不到。"
            "（2026-09-21：默认 240 → 120；2026-09-22：120 → 60，配合日上限 16。）"
        ),
        json_schema_extra={"label": "生活片段间隔", "hint": "分钟；30-1440；需 ≤60 才吃满日上限 16", "order": 9},
    )
    life_fragment_daily_max: int = Field(
        default=16,
        ge=0,
        le=24,
        description=(
            "每日生活片段生成上限（不含「起床补一段」，后者是状态转换的必然产物、"
            "豁免本上限）。成本控制手段之一（另有每日一次的编年史压缩）。"
            "（2026-09-21：默认 3 → 6；2026-09-22：6 → 16，上限校验 12 → 24。"
            "实测每日调用恒被压在上限上，成本仍远低于总量 1%。）"
        ),
        json_schema_extra={"label": "生活片段日上限", "hint": "0-24；0=不生成", "order": 10},
    )
    life_fragment_detail_enabled: bool = Field(
        default=True,
        description=(
            "按事件重要度分级创作详略（2026-09-21 新增）。开启后生活片段分三档："
            "minor（无素材，40~90 字）／normal（有素材，80~180 字）／"
            "major（命中里程碑·状态极端·素材密集，200~400 字，要求写细）。"
            "关闭则回到旧行为（一律 40~90 字，渲染引用仍截 56 字）。"
        ),
        json_schema_extra={
            "label": "分级详略",
            "hint": "关=旧行为（统一 40~90 字）",
            "order": 11,
        },
    )

    # ── 睡眠态（v0.1.10，2026-09-22）─────────────────────────────────────
    # 真源说明：自我层 state 里也有 sleep_time / wake_time 两个键，但那是 v0.1 遗留的
    # **死字段**（全仓从未读取），本段配置才是唯一真源；state 里那两个键保留只为
    # 兼容旧存档，不再被任何代码读取。
    sleep_time: str = Field(
        default="23:30",
        description=(
            "入睡时刻（HH:MM）。到点进入睡眠态：停止生成生活片段、不主动开口、每 tick 回精力；"
            "深夜收到消息会「被吵醒」（瞬时，不改变睡眠状态，只是这一轮有点迷糊）。"
            "⚠️ 留空 = 关闭整个睡眠态（bot 全天不睡，回 v0.1.9 及以前的行为）。"
        ),
        json_schema_extra={
            "label": "入睡时刻",
            "hint": "HH:MM；留空=不睡觉（关闭睡眠态）",
            "order": 12,
        },
    )
    wake_time: str = Field(
        default="07:00",
        description=(
            "起床时刻（HH:MM）。醒来时补写一段「刚醒」的生活片段"
            "（豁免间隔闸门与日上限；受 wake_fragment_enabled 约束）。"
        ),
        json_schema_extra={"label": "起床时刻", "hint": "HH:MM", "order": 13},
    )
    sleep_delay_max_minutes: int = Field(
        default=60,
        ge=0,
        le=240,
        description=(
            "入睡推迟上限（分钟）：到 sleep_time 时若仍在聊天，则推迟入睡，最多推迟本值；"
            "超过上限（或已不再聊天）则强制入睡。0 = 到点即睡，不看是否还在聊。"
            "「仍在聊」的判定窗口见 sleep_delay_recent_minutes。"
        ),
        json_schema_extra={"label": "入睡推迟上限", "hint": "分钟；0=到点即睡", "order": 14},
    )
    sleep_delay_recent_minutes: int = Field(
        default=10,
        ge=0,
        le=120,
        description="判定「仍在聊」的互动新鲜度窗口（分钟）：最近一次互动落在该窗口内即推迟入睡。",
        json_schema_extra={"label": "仍在聊判定窗口", "hint": "分钟；默认 10", "order": 15},
    )
    woken_awake_minutes: int = Field(
        default=30,
        ge=0,
        le=240,
        description=(
            "被吵醒的持续时长（分钟）：深夜收到消息后，在这段时间内注入「被吵醒」提示，"
            "且不再享受睡眠精力恢复。到期自动回落——不改变 sleep_state（瞬时语义）。"
        ),
        json_schema_extra={"label": "被吵醒持续", "hint": "分钟；默认 30", "order": 16},
    )
    energy_woken_penalty: float = Field(
        default=0.08,
        ge=0.0,
        le=1.0,
        description=(
            "深夜被吵醒一次的精力扣减。一夜多次会叠加，但有地板保护"
            "（不低于 energy_woken_floor），避免连环消息把精力打穿。"
        ),
        json_schema_extra={"label": "被吵醒扣减", "hint": "每次扣减量；默认 0.08", "order": 17},
    )
    energy_woken_floor: float = Field(
        default=0.3,
        ge=0.05,
        le=1.0,
        description="被吵醒扣减的地板：一夜被吵醒多次时，精力不会低于此值。",
        json_schema_extra={"label": "被吵醒地板", "hint": "0.05-1.0；默认 0.3", "order": 18},
    )
    wake_fragment_enabled: bool = Field(
        default=True,
        description=(
            "起床补一段：醒来时立刻写一段「刚醒」的生活片段（豁免间隔闸门与日上限）。"
            "存在的理由——睡眠期间不生成片段，若不补，早上所有用户拿到的由头都会是"
            "昨晚睡前那同一条。"
        ),
        json_schema_extra={"label": "起床补一段", "hint": "关=醒来不额外生成", "order": 19},
    )
    sleep_pre_sleep_hint_minutes: int = Field(
        default=25,
        ge=0,
        le=180,
        description=(
            "临近入睡提示窗口（分钟）：距 sleep_time 不足本值时，对话注入追加"
            "「你有点困了」的提示。0 = 不加该提示。"
        ),
        json_schema_extra={"label": "临近入睡提示", "hint": "分钟；0=关闭；默认 25", "order": 20},
    )


class UserWindowRule(PluginConfigBase):
    """按用户活跃窗口规则：一个 QQ 号 × 若干星期几 × 一个时段。

    为什么从 ``user_active_windows: dict`` 换成结构化列表：
    裸 ``dict``（以及 ``dict[str, X]``）在 WebUI 插件配置页会落到 FieldRenderer 的
    default 分支 → 单行输入框渲染成 ``[object Object]``，**无法编辑**。
    ``List[PluginConfigBase 子类]`` 则会被 SDK 展开成带标签的卡片行（可增删），
    彻底消灭引号/冒号/括号/分隔符的手写语法。
    """

    user_id: str = Field(
        default="",
        description="QQ 号（纯数字）。留空或非法则该条永不命中。",
        json_schema_extra={
            "label": "QQ 号",
            "hint": "纯数字，例 3892809830",
            "placeholder": "3892809830",
            "order": 1,
        },
    )
    days: List[Literal["1", "2", "3", "4", "5", "6", "7"]] = Field(
        default_factory=lambda: ["1", "2", "3", "4", "5", "6", "7"],
        description="生效星期（ISO 记法：1=周一 … 7=周日）。全部取消勾选 = 永不主动。",
        json_schema_extra={
            "label": "生效星期",
            "hint": "1=周一、2=周二 … 7=周日；可多选",
            "order": 2,
        },
    )
    start: str = Field(
        default="09:00",
        description="开始时刻（HH:MM）。",
        json_schema_extra={"label": "开始时刻", "hint": "HH:MM，例 20:20", "order": 3},
    )
    end: str = Field(
        default="22:00",
        description="结束时刻（HH:MM）。支持跨天（如 20:20-05:00）。",
        json_schema_extra={
            "label": "结束时刻",
            "hint": "HH:MM；跨天例：开始 20:20 / 结束 05:00",
            "order": 4,
        },
    )


class ProactiveSection(PluginConfigBase):
    """主动消息调度设置（事件驱动 + 活跃窗口 + 随机计时 + 静默时段）。"""

    __ui_label__: ClassVar[str] = "主动消息"
    __ui_icon__: ClassVar[str] = "send"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(
        default=False,
        description=(
            "主动消息总开关。关闭时 bot 不会主动开口。"
            "⚠️ 还需同时开启 [plugin].enabled 与 [narrative].enabled 才会真正生效。"
        ),
        json_schema_extra={"label": "启用主动消息", "order": 1},
    )
    silent_start: str = Field(
        default="23:00",
        description="静默开始（HH:MM）。静默期内不主动开口。",
        json_schema_extra={"label": "静默开始", "hint": "HH:MM", "order": 2},
    )
    silent_end: str = Field(
        default="08:00",
        description="静默结束（HH:MM）。",
        json_schema_extra={"label": "静默结束", "hint": "HH:MM", "order": 3},
    )
    random_minutes: List[int] = Field(
        default_factory=lambda: [60, 240],
        description="活跃窗口内随机开口间隔范围（分钟）。",
        json_schema_extra={
            "label": "随机间隔范围",
            # 只取前两项且须 最小≤最大，否则静默回落 60-240
            "hint": "[最小, 最大] 分钟；只取前两项，须 最小≤最大，否则回落 60-240",
            "item_type": "number",
            "order": 4,
        },
    )
    daily_max: int = Field(
        default=2,
        ge=0,
        le=10,
        description="每用户每日主动消息上限（防骚扰）。",
        json_schema_extra={"label": "每日上限", "hint": "0-10", "order": 5},
    )
    default_active_window: List[str] = Field(
        default_factory=lambda: ["09:00-22:00"],
        description="默认活跃窗口（HH:MM-HH:MM）。",
        json_schema_extra={
            "label": "默认活跃窗口",
            "hint": '例 ["09:00-22:00"]',
            "item_type": "string",
            "order": 6,
        },
    )
    user_window_rules: List[UserWindowRule] = Field(
        default_factory=list,
        description=(
            "按用户窗口规则（覆盖默认窗口）。一条规则 = QQ 号 × 生效星期 × 时段；"
            "同一 QQ 可配多条（如工作日一条、周末一条）。"
            "某 QQ 只要有规则，就**只**按规则走，不再使用默认活跃窗口；"
            "星期全不勾选 = 对该用户永不主动。"
        ),
        json_schema_extra={
            "label": "按用户窗口规则",
            "hint": "一条一行：QQ 号 / 生效星期(1=周一…7=周日) / 开始 / 结束",
            "order": 7,
        },
    )
    # ── 分享欲 share_urge（v0.1.8 第一步：动机驱动时机，规则层零 LLM） ──
    # 设计：随机计时器只作"最小间隔闸门"，到点后按分享欲采样决定是否真的开口。
    # 合成 = self 层基线漂移 × branch 层对人系数 × 精力因子（相乘，clamp [0,1]）。
    # 只作用于主动开口时机；用户主动来找时回复路径绝不设门（访谈启发⑦边界）。
    urge_enabled: bool = Field(
        default=True,
        description=(
            "分享欲采样开关（share_urge 第一步）。开启后到点不必然开口，"
            "而是按分享欲采样；关闭则回到旧行为（到点必发）。"
            "只影响 bot 主动找你的时机，不影响你找它时的回复。"
        ),
        json_schema_extra={"label": "分享欲采样", "order": 8},
    )
    urge_base: float = Field(
        default=0.7,
        ge=0.0,
        le=1.0,
        description="self 层基线分享欲：无事件时分享欲每 tick 向该值回归（双向）。",
        json_schema_extra={"label": "基线分享欲", "hint": "0-1；默认 0.7", "order": 9},
    )
    urge_gain: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="正反馈步长：主动消息被接住 +1×该值；用户主动发起对话 +0.5×该值。",
        json_schema_extra={"label": "正反馈步长", "hint": "0-1；默认 0.1", "order": 10},
    )
    urge_decay: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="负反馈步长：主动消息超 30 分钟未被回复（被冷落）时每条 -该值。",
        json_schema_extra={"label": "冷落衰减", "hint": "0-1；默认 0.25", "order": 11},
    )
    urge_regain: float = Field(
        default=0.15,
        ge=0.0,
        le=1.0,
        description="基线回归系数：每 tick 分享欲向基线靠拢的比例（同精力基线回归语义）。",
        json_schema_extra={"label": "基线回归系数", "hint": "0-1；默认 0.15", "order": 12},
    )
    urge_branch_floor: float = Field(
        default=0.4,
        ge=0.0,
        le=1.0,
        description="branch 层对人系数下限：长期被冷落也不会低于该值（防彻底饿死）。",
        json_schema_extra={"label": "对人系数下限", "hint": "0-1；默认 0.4", "order": 13},
    )


class LLMSection(PluginConfigBase):
    """模型路由（按已注册模型名，非 task 名）。"""

    __ui_label__: ClassVar[str] = "模型路由"
    __ui_icon__: ClassVar[str] = "cpu"
    __ui_order__: ClassVar[int] = 4

    creation_model: str = Field(
        default="",
        description=(
            "创作模型名（须与主程序已注册的模型名完全一致，WebUI 模型列表可查看复制）。"
            "只填模型名，无需把模型分配给任何任务。"
            "留空则使用主程序默认模型。推理模型可在该模型的 extra_params 配 "
            "{thinking = {type = \"disabled\"}} 关思考，无需直连。"
        ),
        json_schema_extra={
            "label": "创作模型（模型名）",
            "hint": "已注册模型名；留空用默认模型",
            "placeholder": "my-thinking-off-model",
            "order": 1,
        },
    )
    creation_max_tokens: int = Field(
        default=1024,
        ge=64,
        le=8192,
        description=(
            "创作最大输出 token（默认 1024）。"
            "（2026-09-21 由 384 上调：生活片段分档后 major 档可达 400 字，384 会截断正文。）"
            "v0.2.0 批 1 随 [creator_model] 直连退役（R10）从该段迁移至此。"
        ),
        json_schema_extra={"label": "最大输出 token", "hint": "64-8192", "order": 2},
    )
    temperature: float = Field(
        default=0.9,
        ge=0.0,
        le=2.0,
        description="创作温度。",
        json_schema_extra={"label": "温度", "hint": "0-2", "order": 3},
    )
    show_prompt: bool = Field(
        default=False,
        description="是否在日志打印创作 prompt（调试用）。当前作用于生活片段与编年史压缩两处。",
        json_schema_extra={"label": "日志打印 prompt", "order": 4},
    )


class PromotionSection(PluginConfigBase):
    """慢变晋升机（批 4）：门槛 / 冷却 / 反证 / 投影限流。

    ⚠️ 本段**不承载学习成果**——`[learned]` 区块刻意不声明进 config.py
    （ADR-0002 决策 6：声明进去就会进 WebUI 表单，用户保存配置时表单以 **stale 值
    整体回写**，冲掉插件写入的学习成果）。本段只管**晋升行为**的参数。

    ⚠️ 关系晋升的证据源是 ``metrics/*.csv``（用户主动发起 + 主动消息承接），
    而 csv 落盘受 ``[telemetry].enabled`` **与** ``[plugin].enabled`` 双门控：
    **关掉验收采样 = 关掉关系晋升的证据源**。这是刻意的取舍（不为此另建事件表，
    见批 4 方案 §0.4）；晋升读到 0 条时会 WARN 一次提示该耦合。
    """

    __ui_label__: ClassVar[str] = "慢变晋升"
    __ui_icon__: ClassVar[str] = "trending-up"
    __ui_order__: ClassVar[int] = 5

    enabled: bool = Field(
        default=True,
        description="是否启用慢变晋升机（提案提炼 + 晋升判定 + 写回投影）。",
        json_schema_extra={"label": "启用晋升机", "order": 1},
    )
    seed_on_start: bool = Field(
        default=True,
        description=(
            "冷启动 seed（R15）：首次上线时从锚定层 world/values 一次性派生 "
            "world_view / life_goals 初值，作为晋升 diff 的基线。"
            "失败留空 + WARN，不阻断。默认开——不设 seed 则「现值」不存在，"
            "「她的看法何时变过」无从叙述（冷启动死锁）。"
        ),
        json_schema_extra={"label": "冷启动 seed", "order": 2},
    )
    interval_hours: int = Field(
        default=168,
        ge=1,
        le=720,
        description="提案提炼间隔（小时）。默认 168 = 周度（HDSI 3.1 口径）。",
        json_schema_extra={"label": "提炼间隔（小时）", "hint": "1-720；默认 168", "order": 3},
    )
    failure_backoff_hours: int = Field(
        default=6,
        ge=1,
        le=72,
        description=(
            "提炼失败后的退避（小时）。**重启后必再试一次**——失败指纹不持久化，"
            "只有冷却跨重启（HDSI 5.3：防「重启即重试风暴」的同时不放过真失败）。"
        ),
        json_schema_extra={"label": "失败退避（小时）", "hint": "1-72", "order": 4},
    )
    min_evidence_entries: int = Field(
        default=5,
        ge=1,
        le=200,
        description=(
            "输入充分性门槛：证据条目少于此值**直接不调 LLM**。"
            "既省成本，也避免「输入不充分 → 产出恒空 → 功能形同虚设」（HDSI 5.9）。"
        ),
        json_schema_extra={"label": "最少证据条数", "hint": "默认 5", "order": 5},
    )
    max_proposals: int = Field(
        default=8,
        ge=1,
        le=50,
        description="单次提炼最多采纳的提案条数（超出丢弃，防一次写爆）。",
        json_schema_extra={"label": "单次提案上限", "order": 6},
    )

    minor_confidence: float = Field(
        default=0.82,
        ge=0.0,
        le=1.0,
        description="minor 晋升的置信门槛（P3）。",
        json_schema_extra={"label": "minor 置信门槛", "hint": "0-1；默认 0.82", "order": 7},
    )
    minor_min_scenes: int = Field(
        default=3,
        ge=1,
        le=50,
        description="minor 晋升要求的最少独立场景数（P3）。",
        json_schema_extra={"label": "minor 最少场景", "hint": "默认 3", "order": 8},
    )
    minor_min_days: int = Field(
        default=2,
        ge=1,
        le=90,
        description="minor 晋升要求跨越的最少自然日数（P3）。",
        json_schema_extra={"label": "minor 最少跨日", "hint": "默认 2", "order": 9},
    )
    cooldown_hours: int = Field(
        default=72,
        ge=0,
        le=720,
        description=(
            "同一路径的晋升冷却（小时，P3）。**跨重启持久化**（kv 存 ISO 时间戳）——"
            "HDSI 5.3 导演冷却口径：防同一维度被反复推进。"
        ),
        json_schema_extra={"label": "晋升冷却（小时）", "hint": "默认 72", "order": 10},
    )
    major_enabled: bool = Field(
        default=False,
        description=(
            "是否启用 major 晋升（P4）。默认**关**——major 允许「高置信但场景少」的"
            "跃迁，风险与收益不对称，先观察 minor 的实际节奏再开。"
        ),
        json_schema_extra={"label": "启用 major 晋升", "order": 11},
    )
    major_confidence: float = Field(
        default=0.95,
        ge=0.0,
        le=1.0,
        description="major 晋升的置信门槛（P4）。",
        json_schema_extra={"label": "major 置信门槛", "hint": "0-1；默认 0.95", "order": 12},
    )
    major_min_scenes: int = Field(
        default=2,
        ge=1,
        le=50,
        description="major 晋升要求的最少场景数（不限天数，P4）。",
        json_schema_extra={"label": "major 最少场景", "hint": "默认 2", "order": 13},
    )
    refutation_penalty: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description=(
            "反证命中时扣减的置信（P6）：``confidence − penalty`` 低于门槛即 "
            "``status=rejected``。反证草稿本身**不留存**为新候选（防自我强化）。"
        ),
        json_schema_extra={"label": "反证扣减", "hint": "0-1；默认 0.2", "order": 14},
    )
    merge_bonus: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description=(
            "重复提案合并时的信度加成（P7）：``merged = min(本次, 旧值 + bonus)``，"
            "且**仅当来自新的独立场景才加**——复述不加信（HDSI 2.1）。"
        ),
        json_schema_extra={"label": "合并信度加成", "hint": "默认 0.05", "order": 15},
    )

    relation_min_days: int = Field(
        default=3,
        ge=1,
        le=365,
        description=(
            "关系确定性晋升的起步门槛（E14）：累计正向场景日达到该值才可能首次晋升。"
            "正向场景日 = 「用户主动发起」∪「主动消息被承接」按自然日去重。"
        ),
        json_schema_extra={"label": "关系起步场景日", "hint": "默认 3", "order": 16},
    )
    relation_days_per_step: int = Field(
        default=2,
        ge=1,
        le=30,
        description="每新增多少个正向场景日提升一档（E14）。",
        json_schema_extra={"label": "关系每档场景日", "hint": "默认 2", "order": 17},
    )
    relation_step: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="关系维度每档的提升幅度（E14）。",
        json_schema_extra={"label": "关系档位步长", "hint": "默认 0.05", "order": 18},
    )
    relation_max: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "关系维度确定性晋升的上限（E14）。留出余量给批 5 的 LLM 提案与人工调整——"
            "确定性计数不该把关系推到顶格。"
        ),
        json_schema_extra={"label": "关系上限", "hint": "0-1；默认 0.8", "order": 19},
    )

    projection_limit: int = Field(
        default=2,
        ge=0,
        le=10,
        description=(
            "读取端限流（R28 / P8）：注入的慢变软倾向**最多**几条（按相关度排序）。"
            "HDSI 3.1 口径默认 2。"
        ),
        json_schema_extra={"label": "软倾向注入上限", "hint": "默认 2", "order": 20},
    )


class TelemetrySection(PluginConfigBase):
    """验收采样（5 指标，见 .scratch/narrative-persona/acceptance-dashboard.md）。"""

    __ui_label__: ClassVar[str] = "验收采样"
    __ui_icon__: ClassVar[str] = "activity"
    __ui_order__: ClassVar[int] = 6

    enabled: bool = Field(
        default=True,
        description=(
            "是否写入 metrics CSV（data/plugins/glcoge.mai-narrative/narrative/metrics/）。"
            "⚠️ 还需 [plugin].enabled=true 才会真正写入。"
            "关闭只影响 CSV 落盘，跟踪状态照常更新（A/B 口径依赖此语义）。"
        ),
        json_schema_extra={"label": "启用采样", "order": 1},
    )


class MaiNarrativePluginConfig(PluginConfigBase):
    """mai-narrative 顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    anchor: AnchorSection = Field(default_factory=AnchorSection)
    narrative: NarrativeSection = Field(default_factory=NarrativeSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    promotion: PromotionSection = Field(default_factory=PromotionSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    telemetry: TelemetrySection = Field(default_factory=TelemetrySection)


__all__ = [
    "UserWindowRule",
    "PluginSection",
    "IdentitySection",
    "AnchorSection",
    "NarrativeSection",
    "ProactiveSection",
    "PromotionSection",
    "LLMSection",
    "TelemetrySection",
    "MaiNarrativePluginConfig",
]