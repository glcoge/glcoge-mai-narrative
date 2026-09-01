# MaiBot 剧本人设系统（glcoge-mai-narrative）

让 bot 活在实时推进的剧本里：有生活、有情绪、会主动开口、随生活自进化。
v0.1 最小切片：**单人单私聊"剧本模式"**（先只让你的 QQ 参与）。

设计文档与验收仪表盘见仓库 `.scratch/narrative-persona/`。

## 它做什么

| 能力 | 机制 |
|---|---|
| 剧本状态 | 双层状态机：自我层（全局心情/作息/聚焦）+ 支线层（每用户关系值/里程碑） |
| 生活推进 | 世界时钟规则 tick（30min，零 LLM）+ 每日编年史压缩（轻量模型一次） |
| 主动开口 | 活跃窗口 + 随机 1~4h 计时 + 23:00~08:00 静默 + 由头签发（有由头才开口） |
| 对话注入 | 每轮请求前注入剧本生活状态（`maisaka.planner.before_request`） |
| 表达学习隔离 | 剧本模式会话阻断表达注入/写入（防稀释人设） |
| 验收采样 | 5 指标 CSV（用户主动频率/对话深度/主动回复率/状态多样性/额外成本） |

## 启用步骤

1. **写人设（在主配置）**：把 `config/bot_config.toml` 的 `[personality]` 三段（`personality` 身份白描 / `behavior_style` 行为准则 / `reply_style` 说话风格）改成剧本人设——人设主体**复用原生配置**，系统提示里只保留这一份"你是谁"，避免双人格。**性格/语气/行为一律只写在这里**，插件不重复定义。
2. WebUI 插件页启用本插件，填写 `[identity]` **锚定层**（只填原生三段没有的维度）：`world` 世界观、`values` 价值观底线、`world_rules` 世界观规则/禁忌。
3. `[narrative] mode_user_ids` 填入你的 QQ 号；`[narrative] enabled = true`。
4. 需要主动开口时：`[proactive] enabled = true`，并按需调窗口/每日上限。
5. （v0.1.3）创作层默认开启：`[narrative] life_fragment_interval_minutes = 240`（生活片段生成间隔，分钟）、`life_fragment_daily_max = 3`（每日上限）。间隔越短、上限越高，主动消息由头越"有生活"，token 成本也越高——**先按默认值跑，观察指标 5 再收紧**。
6. 保存配置（热重载自动生效）；重启后任务自动重排。
7. 私聊里 `/narrative status` 查看状态；`/narrative reset yes` 重置状态（编年史保留）。

## 命令 / API

- `/narrative help` `/narrative status` `/narrative reset [yes]`（管理员 QQ 白名单）
- API `narrative_state`（仅元信息，不含正文；非公开，仅本插件场景使用）
- API `narrative_diary_context`（**公开**，供 mai-diary 握手）：剧本会话判定数据 + 自我层人格摘要
- API `narrative_chronicle_append`（**公开**，供 mai-diary 握手）：幂等写自我层编年史（同一天不重写）

## 约束（设计铁律）

- **不改主程序代码**：全部通过插件挂载点实现（Hook/Event/Command/API）。
- **锚定层不可改**：`[identity]` 只读，代码永不改写；里程碑只进不退。
- **LLM 只在规则候选里创作**：tick 纯规则；每日仅一次编年史压缩调 LLM（`llm.creation_task`）。
- **编年史 append-only**：重置也不清编年史。

## 创作模型路由（v0.1.3，重要）

生活片段 / 编年史压缩默认走 `[llm] creation_task`（MaiBot task 路由）。⚠️ **若该任务用的模型是推理模型（如 Mimo V2.5）且未关思考，思维链会挤占 `max_tokens` 导致正文截断**（日志出现 "超过 max_token 限制"）。

**推荐：插件直连创作模型**（`[creator_model]` 段，纯 WebUI 填 4 项）——插件自己 POST 到 OpenAI 兼容端点，**body 固定携带 `thinking={type:"disabled"}`**，彻底绕开思维链与任务路由：

1. 本插件配置页 → `创建模型直连` 段：
   - `enabled = true`
   - `base_url` = 服务商 OpenAI 兼容地址（如 `https://…/v1`，自动拼 `/chat/completions`）
   - `api_key` = 你的 Key
   - `model_id` = 模型 ID（如 `mimo-v2.5`）
   - `max_tokens` = 384（默认，正文 40~90 字足够且有余量）
2. 生活片段/编年史即走直连（该模型是否推理、是否开思考都无所谓——thinking 被强制关闭）。

要点：
- 直连启用后**不依赖 MaiBot 模型体系**：不占任务、不经 RPC、不改 model_config.toml。
- `[llm] creation_task` 仅在 `[creator_model].enabled=false` 或 `base_url` 空时作为回退。
- API Key 明文存插件配置（与 model_config.toml 现状一致）；如需更安全可后续改环境变量引用。
- 已知 MaiBot 缺陷（llm.generate 的 model_name 被 host 吞入任务名解析）：见 `E:\1MyProjects\maibot-model-name-issue-pr-template.md`，可提交 issue/PR 打通原生通道。

## 数据目录

`data/plugins/glcoge.mai-narrative/narrative/`
- `narrative.db`：状态 kv / 编年史 / 事件队列（sqlite）
- `snapshots/YYYY-MM-DD.json`：每日状态快照（回滚点 + 状态多样性指标）
- `metrics/*.csv`：验收采样

## 与日记插件（glcoge-mai-diary）的握手

已实施（2026-08-30，mai-diary v1.3.0 适配）：

- 日记生成时经 `narrative_diary_context` 分诊：剧本模式会话 → 作者人格改用自我层；
  否则沿用全局 `personality.*`。
- 日记成功落盘后经 `narrative_chronicle_append` 幂等写入自我层编年史
  （`scope=self, kind=diary`；同一天重跑不重复写；`chronicle_enabled=false` 时自动跳过）。
- 任一步失败（narrative 未加载/未启用/调用异常）日记侧自动降级回旧逻辑，互不阻塞。