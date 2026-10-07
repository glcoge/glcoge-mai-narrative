"""世界知识基座 loader（v0.3.0 批 1 / R43 / 总览 §6.6 / grill 定案 2026-09-05）。

**定位裁定**（防走样，见 `.workbuddy/v0.3重构/批1执行方案.md` §0.2）：

- Lorebook 是生活层的**世界知识素材基座**，不是人格第四层。与锚定层的分界：
  锚定层管「**她是谁**、底线是什么」（config，用户手写、运行时只读、永不改写）；
  Lorebook 管「**她的世界有什么**」（world 世界风景 / cast NPC 名册 / entry 关键词条目）。
- **NPC = 生活层私有剧场素材，不是社交层实体**：``cast`` 条目只被批 2 播种器消费；
  真实用户永不进世界书、NPC 永不进社交层关系账本（三源分工）——批 2
  「参与者禁入」红线的另一面。
- detailed 模式下 ``[identity].world`` 不注入（单一事实源，防双世界观并置）；
  ``values`` / ``world_rules`` **两种模式都注入、逻辑不动**（铁律留在锚定层）。

本包只做**登记与读取**（TOML 加载 / 触发匹配 / 预算截断），不做生成——
生成属批 2 世界事件源（播种器），``list_cast()`` 是它的取材面。
"""

from .loader import LorebookEntry, LorebookLoader

__all__ = ["LorebookEntry", "LorebookLoader"]
