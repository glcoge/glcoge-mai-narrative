"""回放台用例集合（按模块聚合，供 run.py 发现）。"""

from .case_privacy_leak import case_diary_fully_isolated, case_privacy_leak

CASES = {
    "privacy_leak": case_privacy_leak,
    "diary_isolated": case_diary_fully_isolated,
}

__all__ = ["CASES", "case_privacy_leak", "case_diary_fully_isolated"]
