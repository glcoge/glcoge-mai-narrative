"""学习层：`[learned]` 投影读写（批 3）、规则调制（批 3）、慢变提案与证据（批 4）。

- ``drift_style.py``：漂移层规则调制（mood/energy/作息 → 表达提示），批 3 落地
- ``projection.py``：``[learned]`` 区块 tomlkit 直读写 + 防重入，批 3 落地（ADR-0002 决策 6）
- ``proposal.py``：慢变提案通道（LLM 周度提炼 + 受控维度契约），批 4 落地
- ``evidence.py``：晋升证据的代码级准入（**白名单 kind** + 来源桶掩码 + 事件级正向信号），批 4 落地
- ``pairs.py``：互动配对（「反馈 id → 已送达回应 id」），批 4 **只建不消费**（R31）

⚠️ 依赖方向：``evidence`` 依赖 ``render.audience`` 与 ``state.continuity`` 的
**声明常量**（不出上层）；``pairs`` 是**孤岛**——晋升链路禁止 import 它（R31）。

见 ADR-0002 / ADR-0003。
"""
