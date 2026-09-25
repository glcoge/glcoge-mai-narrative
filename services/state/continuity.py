"""三层连续性 —— 批 2/批 4 实现（ADR-0002）。

批 2：受控白名单 schema + proposals/promotions 两表接入 + schema_version。
批 4：晋升状态机（门槛/冷却/反证/证据纪律五条）+ 留痕 + 回滚。
批 0 空占位：零行为。
"""
