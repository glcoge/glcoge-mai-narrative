"""回放台用例集合（按模块聚合，供 run.py 发现）。

用例签名统一为 ``fn(inputs: loader.ReplayInputs) -> List[str]``（返回失败列表，
空 = 通过）。批 4-C11 起需要归档库/指标目录的用例从 ``inputs.db`` /
``inputs.metrics_dir`` 取路径——路径一律由 CLI 传入，不入库。
"""

from .case_evidence_counting import case_evidence_counting, case_scene_mapping_digest
from .case_privacy_leak import case_diary_fully_isolated, case_privacy_leak
from .case_promotion import (
    case_input_sufficiency,
    case_promotion_rhythm,
    case_positive_signal,
)
from .case_style_injection import case_style_injection

CASES = {
    "privacy_leak": case_privacy_leak,
    "diary_isolated": case_diary_fully_isolated,
    "evidence_counting": case_evidence_counting,
    "scene_mapping_digest": case_scene_mapping_digest,
    "style_injection": case_style_injection,
    "promotion_rhythm": case_promotion_rhythm,
    "input_sufficiency": case_input_sufficiency,
    "positive_signal": case_positive_signal,
}

__all__ = [
    "CASES",
    "case_diary_fully_isolated",
    "case_evidence_counting",
    "case_input_sufficiency",
    "case_privacy_leak",
    "case_promotion_rhythm",
    "case_positive_signal",
    "case_scene_mapping_digest",
    "case_style_injection",
]
