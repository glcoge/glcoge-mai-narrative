# MaiBot 剧本人设系统（glcoge-mai-narrative）

让 bot 活在实时推进的剧本里：有生活、有情绪、会主动开口、随生活自进化。
v0.1 最小切片：**单人单私聊"剧本模式"**（先只让你的 QQ 参与）。

设计文档与验收仪表盘在**主仓库** `.scratch/narrative-persona/`（本插件为独立仓库，不含此目录）。

## 它做什么

| 能力 | 机制 |
|---|---|
| 剧本状态 | 双层状态机：自我层（全局心情/作息/聚焦）+ 支线层（每用户关系值/里程碑） |
| 生活推进 | 世界时钟规则 tick（30min，零 LLM）+ 创作层生活片段生成（间隔+日上限双闸门）+ 每日编年史压缩 |
| 主动开口 | 活跃窗口（可按用户 × 按星期） + 随机 1~4h 计时 + 23:00~08:00 静默 + 由头签发（有由头才开口，不干聊） |
| 对话注入 | 每轮请求前注入剧本生活状态（`maisaka.planner.before_request`，含作息相位 + 日照预期锚点） |
| 表达学习隔离 | 剧本模式会话阻断表达注入/写入（防稀释人设） |
| 验收采样 | 5 指标 CSV（用户主动频率/对话深度（消息长度+往返轮次）/主动回复率/状态多样性/额外成本） |

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
- **LLM 只在规则候选里创作**：tick 纯规则；生活片段 + 编年史压缩按频率闸门调 LLM（见「创作模型路由」）。
- **编年史 append-only**：重置也不清编年史。

## 创作模型路由（v0.1.5 起：按模型名路由，推荐）

生活片段 / 编年史压缩的模型路线（二选一，直连优先）：

**路线 A：按模型名路由（默认，推荐）** —— `[llm] creation_model` 填一个**已在主程序注册的模型名**（WebUI 模型列表可查看复制），只填模型名、**无需把模型分配给任何任务**（MaiBot ≥1.2.5 修复 #2031 后支持按名调用，模型名查全局列表）。留空则用主程序默认模型。

- 推理模型关思考：直接在该模型的 `extra_params` 配 `{thinking = {type = "disabled"}}`（WebUI 模型编辑页可配），插件侧零改动。
- 配置名写错不会静默：调用失败日志会明确报 `未找到名为 'X' 的模型`，并附 `[llm].creation_model` 的值与"去 WebUI 模型列表核对该名称是否已注册"的提示。
- ⚠ `/narrative status` 末尾那行是**宿主可用任务名**（`utils`/`planner`/`replyer`/…），**不是模型名**——宿主未向插件开放"已注册模型名"查询能力（`llm.get_available_models()` 返回的是任务列表）。模型名一律去 WebUI「模型列表」复制。

**路线 B：插件直连**（`[creator_model]` 段）—— 需要独立供应商 / 独立 api_key / 独立额度时才用：插件自己 POST 到 OpenAI 兼容端点，**body 固定携带 `thinking={type:"disabled"}`**：

1. 本插件配置页 → `创作模型直连` 段：
   - `enabled = true`
   - `base_url` = 服务商 OpenAI 兼容地址（如 `https://…/v1`，自动拼 `/chat/completions`）
   - `api_key` = 你的 Key
   - `model_id` = 模型 ID（如 `mimo-v2.5`）
   - `max_tokens` = 384（默认，正文 40~90 字足够且有余量）
2. 生活片段/编年史即走直连（该模型是否推理、是否开思考都无所谓——thinking 被强制关闭）。

要点：
- 直连启用后**不依赖 MaiBot 模型体系**：不占任务、不经 RPC、不改 model_config.toml；适合"创作模型与主模型不同供应商"的场景。
- 直连关闭时走路线 A；`[llm].creation_model` 为空则用主程序默认模型。
- API Key 明文存插件配置（与 model_config.toml 现状一致）；如需更安全可后续改环境变量引用。
- 历史备注：v0.1.3~v0.1.4 的回退路线是 `[llm] creation_task`（task 名路由），因 #2031（model_name 被吞入任务名解析）只能按 task 复用；#2031 已随 MaiBot 1.2.5 修复（维护者提交 `008019c2`），v0.1.5 起改为按模型名路由，`creation_task` 字段废弃。

## 精力规则（v0.1.5 起：基线回归，可调）

v0.1.5 重写了世界时钟的精力规则（修复无互动日 mood 贴地 0.05 卡死）：不再每 tick 无条件衰减，改为**向基线双向回归**——低于基线回升、高于基线回落，另加深夜睡眠恢复与互动提振。`[narrative]` 段四参数可调（旧 config.toml 缺字段自动走默认值，兼容）：

| 参数 | 默认 | 语义 |
|---|---|---|
| `energy_baseline` | 0.45 | 精力基线：无互动时每 tick 向该值回归（双向） |
| `energy_baseline_pull` | 0.3 | 基线回归系数；**0 = 关闭回归（精力只升不降，等于冻结）** |
| `energy_sleep_recovery` | 0.1 | 深夜（23:00-05:00）每 tick 额外恢复量；深夜平衡点 ≈ 基线 + 恢复量/pull ≈ 0.78（轻快档下沿），早晨自然满状态 |
| `energy_interaction_boost` | 0.12 | 近 2 小时内有互动时每 tick 额外提振量 |

纯规则零 LLM，调参只影响生活状态呈现与注入文案；调参后观察 `/narrative status` 与指标 4（状态多样性）再定。

## 主动消息窗口（按用户 × 按星期）

只想在**某些天 / 某些时段**被 bot 主动找（例：工作日白天免打扰，晚上和周末照常），用 `[proactive].user_window_rules`。

**配置形态**：一条规则 = 一个 QQ 号 × 生效星期 × 一个时段，同一 QQ 可写多条。

```toml
[[proactive.user_window_rules]]
user_id = "3892809830"
days = ["1", "2", "3", "4", "5"]   # 工作日
start = "20:20"
end = "05:00"                       # 跨午夜：到次日 05:00

[[proactive.user_window_rules]]
user_id = "3892809830"
days = ["6", "7"]                   # 周末
start = "09:00"
end = "22:00"
```

> 在 WebUI 插件配置页里这是**带标签的卡片行**（点"添加"填 4 个框即可，星期是多选框），
> 不需要手写引号 / 冒号 / 括号 / 分隔符。旧版 `user_active_windows`（裸 dict）在页面上
> 显示成 `[object Object]` 且无法编辑，已废弃——残留该键会在启动日志里 WARN。

| 字段 | 说明 |
|---|---|
| `user_id` | QQ 号（纯数字字符串） |
| `days` | 生效星期，**ISO 记法**：1=周一 2=周二 3=周三 4=周四 5=周五 6=周六 7=周日；可多选 |
| `start` / `end` | `HH:MM`；支持跨午夜（`20:20` → `05:00`）；**右开区间**，结束整点不算 |

四条必须知道的语义：

1. **有规则即覆盖默认窗口**：某 QQ 只要配了规则，就**只**按规则走，不再用 `default_active_window`（不是并集）。没配规则的用户仍走默认窗口。
2. **星期全不勾 = 永不主动**：`days = []` 的规则不会回退到默认窗口——这是"对这个人不主动"的唯一正确写法（旧版空列表反而会回退，已修）。
3. **跨午夜按"当前时刻"的星期算**：周五 20:20 的窗口过了 24:00 即失效，不会被周六规则接着算。想覆盖凌晨就给周六单独写一条。
4. **与全局静默期取交集**：`silent_start`/`silent_end`（默认 23:00~08:00）优先级更高。上例工作日实际可触发区间是 20:20~23:00。

改完**约 30 秒内生效，无需重启**（调度器每 30s 重读配置）。QQ 号非数字 / 星期越界 / 时刻非法会在启动与热重载时打 WARN 并点名第几条，该条运行期被跳过——不会静默失效。

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