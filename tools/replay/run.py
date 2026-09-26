"""回放台 CLI 入口。

用法（数据路径走 CLI，不硬编码、不入库）:

    python tools/replay/run.py --messages E:\\Downloads\\MaiBot-export\\messages.jsonl
    # 批 4-C11：晋升定标用例还需要归档库与指标目录
    python tools/replay/run.py --messages ... \\
        --db "<归档>/narrative/narrative.db" \\
        --metrics-dir "<归档>/narrative/metrics"

退出码：0=全部通过，1=有失败（批 1 的 C1 阶段**期望为 1**，这是 TDD 的红）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List

# 直接以脚本方式运行时，保证能以 ``tools.replay.*`` 绝对导入
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from tools.replay.cases import CASES  # noqa: E402
from tools.replay.loader import ReplayInputs  # noqa: E402


def run_all(
    inputs: ReplayInputs, *, only: str = ""
) -> Dict[str, List[str]]:
    """跑全部（或指定）用例，返回 ``{用例名: 失败列表}``。"""
    results: Dict[str, List[str]] = {}
    for name, fn in CASES.items():
        if only and name != only:
            continue
        try:
            results[name] = list(fn(inputs))
        except Exception as exc:  # noqa: BLE001
            results[name] = [f"用例执行异常：{type(exc).__name__}: {exc}"]
    return results


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="narrative 回放台")
    parser.add_argument(
        "--messages",
        required=True,
        help="MaiBot-export 的 messages.jsonl 路径（归档数据不入库）",
    )
    parser.add_argument(
        "--db",
        default="",
        help="归档 narrative.db 路径（晋升定标用例用；只读副本，绝不写原库）",
    )
    parser.add_argument(
        "--metrics-dir",
        default="",
        help="归档 metrics/ 目录（正向信号用例用；csv 路径由 CLI 传入）",
    )
    parser.add_argument("--case", default="", help="只跑指定用例（默认全部）")
    args = parser.parse_args(argv)

    path = Path(args.messages)
    if not path.exists():
        print(f"[FATAL] 数据文件不存在: {path}")
        return 2

    inputs = ReplayInputs(messages=str(path), db=args.db, metrics_dir=args.metrics_dir)
    results = run_all(inputs, only=args.case)
    failed_total = 0
    for name, failures in results.items():
        if not failures:
            print(f"[PASS] {name}")
            continue
        failed_total += len(failures)
        print(f"[FAIL] {name} —— {len(failures)} 项")
        for line in failures[:20]:
            print(f"        - {line}")
        if len(failures) > 20:
            print(f"        - …… 其余 {len(failures) - 20} 项已省略")

    total = len(results)
    ok = sum(1 for f in results.values() if not f)
    tail = "" if ok == total else f"（{total - ok} 个失败）"
    print(f"\n{ok}/{total} 用例通过{tail}")
    return 0 if failed_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
