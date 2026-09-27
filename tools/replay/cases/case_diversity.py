"""R8 / P11 铺群闸门基线用例（指标 4-B）。

为什么单独一个用例
------------------
铺群闸门写的是「指标 4-B ≥ ?×基线」，但**基线一次都没产出过**：埋点在
``snapshot.note_fragment``（B 轨 ``output_diversity``）里，全仓却没有任何
消费方把它算成一个数。于是闸门永远无法满足，属「挂着但没米下锅」。

本用例就是那个消费方：拿归档 chronicle 里的真实生活片段，按**与生产同尺度**
的算法算出基线值并打印，供 R8 登记、P11 定倍数。

⚠️ 尺度必须与生产可比：生产喂给 ``ngram_diversity`` 的是**最近 30 条**滚动窗口
（``_DIVERSITY_WINDOW``），不是全量。若直接用全量算，样本越多去重率越低，
算出来的「基线」会比生产实际读数系统性偏低 —— 拿来当闸门阈值会误判。
故这里按同样窗口滚动切片，再对各窗口取均值。

数据来源一律 CLI 传入（``--db``），归档数据不入库。
"""

from __future__ import annotations

from typing import Any, List

from .. import loader
from ..runtime import load

#: 定基线所需的最少样本（低于此值，均值没有统计意义）
MIN_SAMPLES = 10

#: 与生产保持一致的滚动窗口（``services/state/snapshot.py:_DIVERSITY_WINDOW``）
DIVERSITY_WINDOW = 30


def case_diversity_baseline(inputs: Any) -> List[str]:
    """产出指标 4-B 的**基线值**并做健全性校验。"""
    if not inputs.has_db():
        return ["未提供 --db（本用例需要归档 narrative.db；数据路径一律 CLI 传入）"]

    snapshot = load("services.state.snapshot")
    evidence = load("services.learning.evidence")

    rows = loader.load_chronicle(inputs.db)
    texts = [
        str(row.get("text") or "").strip()
        for row in evidence.eligible_entries(rows)
        if str(row.get("text") or "").strip()
    ]
    if len(texts) < MIN_SAMPLES:
        return [
            f"样本不足：合法生活片段仅 {len(texts)} 条，"
            f"定基线至少需要 {MIN_SAMPLES} 条"
        ]

    # 与生产同尺度：滚动窗口切片 → 各窗口取均值
    windows = [
        texts[index : index + DIVERSITY_WINDOW]
        for index in range(0, max(1, len(texts) - DIVERSITY_WINDOW + 1))
    ]
    values = [snapshot.ngram_diversity(batch) for batch in windows]
    baseline = sum(values) / len(values)

    failures: List[str] = []
    if baseline <= 0.0:
        failures.append(
            f"基线为 0（{len(texts)} 条样本）—— 文本过短或完全重复，数据源不符预期"
        )
    # 上限健全性：去重率 > 1 不可能，等于 1 说明每个 gram 都唯一（样本极短才会这样）
    if baseline >= 1.0:
        failures.append(
            f"基线为 {baseline:.4f}（等于 1）—— 每个 n-gram 都唯一，"
            "通常是样本太短，不足以定基线"
        )

    days = sorted({str(row.get("ts"))[:10] for row in rows if str(row.get("ts"))[:10]})
    print(
        f"    指标 4-B 基线 = {baseline:.4f}"
        f"（样本 {len(texts)} 条 / 跨 {len(days)} 天 / "
        f"窗口 {DIVERSITY_WINDOW} / {len(windows)} 个切片）"
    )
    return failures


__all__ = ["case_diversity_baseline"]
