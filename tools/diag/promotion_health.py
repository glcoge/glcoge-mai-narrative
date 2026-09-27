"""晋升健康度诊断 CLI（**只读**，不写库、不改配置）。

回答「晋升机是在等时机，还是在空转」。数据路径一律 CLI 传入（与回放台同纪律：
归档/运行时数据不在仓库里，也不硬编码路径）。

用法（项目根）：

    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/tools/diag/promotion_health.py \\
        --db "<运行时>/glcoge.mai-narrative/narrative/narrative.db" \\
        --config "<插件目录>/config.toml"

``--now`` 可注入时刻做回放（ISO 8601），不给即取当前本地时间。
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path
from typing import Any, List

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import tomlkit  # noqa: E402

from config import MaiNarrativePluginConfig  # noqa: E402
from services.learning.health import promotion_health  # noqa: E402
from services.store import NarrativeStore  # noqa: E402

_VERDICT_TEXT = {
    "ready": "✅ ready —— 有提案已过全部门槛，下一个 tick 就该晋升"
    "（若连续多次盘诊都是 ready 却没晋升 → 真 bug，不是时机问题）",
    "cooling": "⏸ cooling —— 有提案但全部卡在冷却（设计内，不是空转）",
    "below_gate": "🟡 below_gate —— 提案未过门槛，看下面的 failed 项",
    "no_proposals": "⚠️ no_proposals —— 当前没有待决提案（提炼没产出 / 已被消费）",
}


def _load_promotion_config(path: Path) -> Any:
    """用**真实配置模型**读门槛值（不另写一份默认值，防诊断与运行时漂移）。"""
    document = tomlkit.loads(path.read_text(encoding="utf-8"))
    config = MaiNarrativePluginConfig(**document.unwrap())
    return config.promotion


def _render(report: dict) -> str:
    lines: List[str] = []
    lines.append(f"晋升健康度 @ {report['now']}")
    lines.append("")
    lines.append(f"结论：{_VERDICT_TEXT.get(report['verdict'], report['verdict'])}")

    last = report.get("last_promotion")
    if last:
        lines.append(
            f"上次晋升：{last['ts']}（{report['age_days']} 天前）"
            f" {last['path']} / {last['reason']}"
        )
    else:
        lines.append("上次晋升：无（从未晋升过）")
    stale_days = report.get("age_days")
    lines.append(
        f"距上次：{stale_days} 天 → "
        f"{'🔴 已越过可疑空转线，需要查' if report['stale'] else '未越线'}"
    )

    gate = report["gate"]
    lines.append("")
    lines.append(
        "门槛：confidence ≥ {} / scenes ≥ {} / days ≥ {} / 冷却 {}h"
        " / major {}".format(
            gate["minor_confidence"],
            gate["minor_min_scenes"],
            gate["minor_min_days"],
            gate["cooldown_hours"],
            "开" if gate["major_enabled"] else "关",
        )
    )
    pool = report["evidence_pool"]
    lines.append(
        f"证据池：entries={pool['entries']} scenes={pool['scenes']} days={pool['days']}"
    )

    lines.append("")
    lines.append("冷却：")
    if not report["cooldowns"]:
        lines.append("  （无）")
    for item in report["cooldowns"]:
        if item["blocked"]:
            lines.append(
                f"  🔒 {item['path']}  锁至 {item['release_ts']}"
                f"（剩 {item['hours_left']}h）"
            )
        else:
            lines.append(f"  🔓 {item['path']}  已解禁（last={item['last_ts'] or '无'}）")

    lines.append("")
    lines.append("待决提案：")
    if not report["pending"]:
        lines.append("  （无）")
    for item in report["pending"]:
        blocking = item["blocking"]
        if blocking == "cooldown":
            tail = "→ 卡：冷却"
        elif blocking == "gate":
            tail = f"→ 卡：{'、'.join(item['failed'])}"
        else:
            tail = "→ 全过，可晋升"
        lines.append(
            "  #{} {} conf={} scenes={} days={} {}".format(
                item["id"],
                item["path"],
                item["confidence"],
                item["scene_count"],
                item["day_count"],
                tail,
            )
        )
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="晋升健康度诊断（只读）")
    parser.add_argument("--db", required=True, help="narrative.db 路径")
    parser.add_argument("--config", required=True, help="插件 config.toml 路径")
    parser.add_argument("--now", default="", help="注入时刻（ISO 8601），用于回放")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"❌ 找不到 db: {db_path}")
        return 2
    config_path = Path(args.config)
    if not config_path.is_file():
        print(f"❌ 找不到 config: {config_path}")
        return 2

    store = NarrativeStore(db_path.parent)
    promotion = _load_promotion_config(config_path)
    now = (
        datetime.datetime.fromisoformat(args.now)
        if args.now
        else datetime.datetime.now()
    )
    print(_render(promotion_health(store, promotion, now=now)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
