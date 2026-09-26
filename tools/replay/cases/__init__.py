"""回放台用例集合（按模块聚合，供 run.py 发现）。"""

from .case_evidence_counting import case_evidence_counting, case_scene_mapping_digest
from .case_privacy_leak import case_diary_fully_isolated, case_privacy_leak

CASES = {
    "privacy_leak": case_privacy_leak,
    "diary_isolated": case_diary_fully_isolated,
    "evidence_counting": case_evidence_counting,
    "scene_mapping_digest": case_scene_mapping_digest,
}

__all__ = [
    "CASES",
    "case_diary_fully_isolated",
    "case_evidence_counting",
    "case_privacy_leak",
    "case_scene_mapping_digest",
]
