"""剧本人设系统插件配置模型（PluginConfigBase 段式定义）。

设计共识（grill-me 会话）：
- 锚定层（identity）由用户手动配置，运行时只读 —— 对应设计树 R4.1"锚定层"。
- 创作模型按**已注册模型名**路由（`[llm].creation_model`），或走 `[creator_model]`
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
        description="深夜（23:00-05:00）每 tick 额外恢复量：睡觉回血，早晨自然满状态。"
        "深夜平衡点 = 基线 + 恢复量/回归系数（默认 0.45+0.1/0.3≈0.78，落在轻快档下沿）。",
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
        default=240,
        ge=30,
        le=1440,
        description=(
            "生活片段生成的间隔（分钟）。创作层每隔 N 分钟可能用轻量模型生成一段"
            "「生活片段」；间隔越大越省 token。"
            "⚠️ 实际检查粒度 = 世界时钟 tick（clock_tick_minutes），"
            "间隔小于 tick 间隔时以 tick 为准。"
        ),
        json_schema_extra={"label": "生活片段间隔", "hint": "分钟；30-1440", "order": 9},
    )
    life_fragment_daily_max: int = Field(
        default=3,
        ge=0,
        le=12,
        description=(
            "每日生活片段生成上限。这是当前**唯一的成本控制手段**"
            "（另有 [daily_chronicle_time] 每日一次的编年史压缩）。"
        ),
        json_schema_extra={"label": "生活片段日上限", "hint": "0-12；0=不生成", "order": 10},
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


class LLMSection(PluginConfigBase):
    """模型路由（按已注册模型名，非 task 名）。"""

    __ui_label__: ClassVar[str] = "模型路由"
    __ui_icon__: ClassVar[str] = "cpu"
    __ui_order__: ClassVar[int] = 4

    creation_model: str = Field(
        default="",
        description=(
            "创作模型名（须与主程序已注册的模型名完全一致，WebUI 模型列表可查看复制）。"
            "只填模型名，无需把模型分配给任何任务。仅当 [creator_model] 直连关闭时生效；"
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
    temperature: float = Field(
        default=0.9,
        ge=0.0,
        le=2.0,
        description="创作温度。",
        json_schema_extra={"label": "温度", "hint": "0-2", "order": 2},
    )
    show_prompt: bool = Field(
        default=False,
        description="是否在日志打印创作 prompt（调试用）。当前作用于生活片段与编年史压缩两处。",
        json_schema_extra={"label": "日志打印 prompt", "order": 3},
    )


class CreatorModelSection(PluginConfigBase):
    """剧本创作模型（可选：插件直连 OpenAI 兼容端点，绕过 MaiBot 任务路由）。

    启用后，生活片段/编年史压缩直接 POST 到 ``base_url``（/chat/completions），
    body 固定携带 ``thinking = {type: "disabled"}``（生成短文本无需思维链；
    推理模型不禁思考会挤占 max_tokens 导致正文截断）。关闭时回退
    ``[llm].creation_model`` 按**模型名**路由。
    """

    __ui_label__: ClassVar[str] = "创作模型直连"
    __ui_icon__: ClassVar[str] = "link"
    __ui_order__: ClassVar[int] = 5

    enabled: bool = Field(
        default=False,
        description="启用插件直连创作模型（关=回退 [llm].creation_model 按模型名路由）。",
        json_schema_extra={"label": "启用直连", "order": 1},
    )
    base_url: str = Field(
        default="",
        description="OpenAI 兼容 BaseURL（会自动拼接 /chat/completions）。",
        json_schema_extra={
            "label": "BaseURL",
            "hint": "例 https://api.example.com/v1",
            "placeholder": "https://…/v1",
            "order": 2,
        },
    )
    api_key: str = Field(
        default="",
        description=(
            "API Key（明文存插件配置，与 MaiBot model_config.toml 现状一致；"
            "x-widget=password 只在 WebUI 上打码显示，不加密 config.toml）。"
        ),
        json_schema_extra={
            "label": "API Key",
            "hint": "Bearer 令牌；留空则不发 Authorization 头",
            "placeholder": "sk-…",
            # 旧写法 "password": True 是死元数据（前端 FieldRenderer 不读它），
            # 真正生效的是 x-widget → ui_type
            "x-widget": "password",
            "order": 3,
        },
    )
    model_id: str = Field(
        default="",
        description="模型 ID（服务商侧标识，如 mimo-v2.5）。",
        json_schema_extra={"label": "模型 ID", "hint": "服务商侧 model 字段", "order": 4},
    )
    max_tokens: int = Field(
        default=384,
        ge=64,
        le=8192,
        description=(
            "最大输出 token（默认 384；创作正文短，384 足够且含余量）。"
            "⚠️ 仅 [creator_model].enabled=true 的直连路径生效；"
            "回退到 [llm].creation_model 按名路由时固定用 256。"
        ),
        json_schema_extra={"label": "最大输出 token", "hint": "64-8192", "order": 5},
    )
    timeout_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=180.0,
        description="直连请求超时（秒）。",
        json_schema_extra={"label": "超时秒", "order": 6},
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
    narrative: NarrativeSection = Field(default_factory=NarrativeSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    creator_model: CreatorModelSection = Field(default_factory=CreatorModelSection)
    telemetry: TelemetrySection = Field(default_factory=TelemetrySection)


__all__ = [
    "UserWindowRule",
    "PluginSection",
    "IdentitySection",
    "NarrativeSection",
    "ProactiveSection",
    "LLMSection",
    "CreatorModelSection",
    "TelemetrySection",
    "MaiNarrativePluginConfig",
]