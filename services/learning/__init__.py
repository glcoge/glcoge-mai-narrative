"""学习层：`[learned]` 投影读写（批 3）、规则调制（批 3）、慢变提案审计（批 4）。

- ``drift_style.py``：漂移层规则调制（mood/energy/作息 → 表达提示），批 3 落地
- ``projection.py``：``[learned]`` 区块 tomlkit 直读写 + 防重入，批 3 落地（ADR-0002 决策 6）
- ``proposal.py``：慢变提案审计，批 4 落地

见 ADR-0002 / ADR-0003。
"""
