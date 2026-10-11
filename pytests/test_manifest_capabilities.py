"""manifest 能力声明一致性测试。

背景（2026-10-11 线上事故）：主动素材注入（方案A）在 scheduler 里调用
``ctx.maisaka.append_context``（能力名 ``maisaka.context.append``），但
``_manifest.json`` 未声明该能力。Host 授权管理器（plugin_runtime/host/
authorization.py）按 manifest 签发能力令牌，未声明即拒——线上连续 7 个
主动轮 ``[E_CAPABILITY_DENIED]``，主动消息全灭。当时的单元测试用 fake ctx
绕过了真实能力层，本地全绿不代表线上能跑。

本文件不做 mock：直接扫运行时代码（plugin.py / config.py / services/）里
所有 ``ctx.maisaka.*`` 调用点，映射到能力名后断言 manifest 已声明。新增
maisaka 调用时须同步补 ``_METHOD_TO_CAPABILITY`` 映射——漏补会让用例以
「未知调用链」失败，指向映射表，而不是静默通过。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_manifest_capabilities.py -q
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]

# ctx.maisaka.<调用链> → manifest 能力名。新增 maisaka 调用时同步补这里。
_METHOD_TO_CAPABILITY = {
    "append_context": "maisaka.context.append",
    "proactive.trigger": "maisaka.proactive.trigger",
}

_CALL_RE = re.compile(r"ctx\.maisaka\.([a-zA-Z_]+(?:\.[a-zA-Z_]+)*)")


def _collect_runtime_sources() -> list[Path]:
    files = [
        _PLUGIN_ROOT / "plugin.py",
        _PLUGIN_ROOT / "config.py",
    ]
    for path in sorted((_PLUGIN_ROOT / "services").rglob("*.py")):
        files.append(path)
    return [path for path in files if path.is_file()]


def _used_capabilities() -> tuple[set[str], set[str]]:
    """扫描源码，返回 (已映射的能力名集合, 未映射的调用链集合)。"""
    used: set[str] = set()
    unknown: set[str] = set()
    for path in _collect_runtime_sources():
        text = path.read_text(encoding="utf-8")
        for match in _CALL_RE.finditer(text):
            chain = match.group(1)
            capability = _METHOD_TO_CAPABILITY.get(chain)
            if capability is None:
                unknown.add(chain)
            else:
                used.add(capability)
    return used, unknown


def _manifest_capabilities() -> set[str]:
    manifest = json.loads((_PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    return set(manifest.get("capabilities", []))


# ===== 回归用例 =====


def test_all_maisaka_calls_mapped():
    """源码里出现映射表不认识的 ctx.maisaka 调用链 → 先补映射再提交。"""
    _, unknown = _used_capabilities()
    assert not unknown, f"未知 ctx.maisaka 调用链，请补 _METHOD_TO_CAPABILITY: {sorted(unknown)}"


def test_used_capabilities_declared_in_manifest():
    """所有实际调用的 maisaka 能力都必须在 _manifest.json 声明（E_CAPABILITY_DENIED 防回归）。"""
    used, _ = _used_capabilities()
    declared = _manifest_capabilities()
    missing = used - declared
    assert not missing, f"以下能力被调用但 manifest 未声明（线上会 E_CAPABILITY_DENIED）: {sorted(missing)}"


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
