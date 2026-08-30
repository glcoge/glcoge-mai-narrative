"""剧本人设系统插件配置模型（PluginConfigBase 段式定义）。

设计共识（grill-me 会话）：
- 锚定层（identity）由用户手动配置，运行时只读 —— 对应设计树 R4.1"锚定层"。
- 分级路由复用 MaiBot 内部 task（replyer/planner/learner/utils）—— 对应 R5。
- v0.1 最小切片：单人单私聊"剧本模式"。
"""

from __future__ import annotations

from typing import ClassVar, List

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件基础设置。"""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件。关闭后所有剧本行为（注入/主动/隔离）都不生效。",
        json_schema_extra={"label": "启用插件", "order": 1},
    )
    config_version: str = Field(
        default="0.1.2",
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
        description="价值观底线：任何话题都必须遵守，不可更改。",
        json_schema_extra={
            "label": "价值观底线",
            "hint": '例 ["不撒谎","尊重每个玩家"]',
            "item_type": "string",
            "order": 2,
        },
    )
    world_rules: List[str] = Field(
        default_factory=list,
        description="世界观规则/禁忌：世界内不可打破的设定。",
        json_schema_extra={
            "label": "世界观规则",
            "hint": '例 ["这个世界没有魔法","本市没有第 13 区"]',
            "item_type": "string",
            "order": 3,
        },
    )
    immutable_traits: List[str] = Field(
        default_factory=list,
        description="不可变人格特征：语气/性格底色的铁律（如与原生 reply_style 冲突，以本字段为最终裁决）。",
        json_schema_extra={
            "label": "不可变人格",
            "hint": '例 ["说话简短","轻微社恐"]',
            "item_type": "string",
            "order": 4,
        },
    )


class NarrativeSection(PluginConfigBase):
    """剧本模式与世界引擎设置。"""

    __ui_label__: ClassVar[str] = "剧本"
    __ui_icon__: ClassVar[str] = "book-open"
    __ui_order__: ClassVar[int] = 2

    enabled: bool = Field(
        default=False,
        description="剧本模式总开关。关闭时插件完全无副作用（仅命令可用）。",
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
        json_schema_extra={"label": "时钟 tick 间隔", "hint": "分钟；5-240", "order": 4},
    )
    timezone_offset_hours: int = Field(
        default=8,
        ge=-12,
        le=14,
        description=(
            "剧本时区偏移（小时，UTC+）。影响作息阶段/注入时间/主动窗口/编年史日期。"
            "服务器时区与本地不一致时必须配置正确，否则作息会错位。"
        ),
        json_schema_extra={
            "label": "时区偏移",
            "hint": "例：UTC+8=8、UTC-5=-5",
            "order": 5,
        },
    )
    event_max_daily: int = Field(
        default=6,
        ge=0,
        le=50,
        description="每日事件队列上限（全局），防止事件刷屏。",
        json_schema_extra={"label": "每日事件上限", "hint": "0-50", "order": 5},
    )
    event_max_per_user_daily: int = Field(
        default=3,
        ge=0,
        le=20,
        description="每用户每日事件上限。",
        json_schema_extra={"label": "每用户事件上限", "hint": "0-20", "order": 6},
    )
    chronicle_enabled: bool = Field(
        default=True,
        description="编年史总开关（append-only 散文日记，供对话注入与日记插件握手）。",
        json_schema_extra={"label": "编年史", "order": 7},
    )
    daily_chronicle_time: str = Field(
        default="23:30",
        description="每日编年史压缩触发时间（HH:MM）。当日有互动时用轻量模型写一条「今日小结」。",
        json_schema_extra={"label": "编年史压缩时间", "hint": "HH:MM；留空=不自动压缩", "order": 8},
    )


class ProactiveSection(PluginConfigBase):
    """主动消息调度设置（事件驱动 + 活跃窗口 + 随机计时 + 静默时段）。"""

    __ui_label__: ClassVar[str] = "主动消息"
    __ui_icon__: ClassVar[str] = "send"
    __ui_order__: ClassVar[int] = 3

    enabled: bool = Field(
        default=False,
        description="主动消息总开关。关闭时 bot 永不主动开口。",
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
            "hint": "[最小, 最大] 分钟",
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
    user_active_windows: dict = Field(
        default_factory=dict,
        description="按用户覆盖活跃窗口。key=QQ 号，value=窗口列表。",
        json_schema_extra={
            "label": "按用户窗口",
            "hint": '例 {"10001": ["10:00-23:00"]}',
            "order": 7,
        },
    )


class LLMSection(PluginConfigBase):
    """模型路由（复用 MaiBot 内部 task）。"""

    __ui_label__: ClassVar[str] = "模型路由"
    __ui_icon__: ClassVar[str] = "cpu"
    __ui_order__: ClassVar[int] = 4

    creation_task: str = Field(
        default="learner",
        description="事件创作/编年史压缩所用模型 task 名（需已存在于主程序 model_config.toml）。",
        json_schema_extra={
            "label": "创作模型 task",
            "hint": "replyer / utils / learner 等；建议用轻量任务控制成本",
            "placeholder": "learner",
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
        description="是否在日志打印创作 prompt（调试用）。",
        json_schema_extra={"label": "日志打印 prompt", "order": 3},
    )


class TelemetrySection(PluginConfigBase):
    """验收采样（5 指标，见 .scratch/narrative-persona/acceptance-dashboard.md）。"""

    __ui_label__: ClassVar[str] = "验收采样"
    __ui_icon__: ClassVar[str] = "activity"
    __ui_order__: ClassVar[int] = 5

    enabled: bool = Field(
        default=True,
        description="是否写入 metrics CSV（data/plugins/glcoge.mai-narrative/metrics/）。",
        json_schema_extra={"label": "启用采样", "order": 1},
    )


class MaiNarrativePluginConfig(PluginConfigBase):
    """mai-narrative 顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    narrative: NarrativeSection = Field(default_factory=NarrativeSection)
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    llm: LLMSection = Field(default_factory=LLMSection)
    telemetry: TelemetrySection = Field(default_factory=TelemetrySection)


__all__ = [
    "PluginSection",
    "IdentitySection",
    "NarrativeSection",
    "ProactiveSection",
    "LLMSection",
    "TelemetrySection",
    "MaiNarrativePluginConfig",
]