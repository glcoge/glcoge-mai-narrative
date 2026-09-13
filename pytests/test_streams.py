"""StreamRegistry 单测（2026-09-13 体检 C4：注册表自 plugin.py 下沉 services/streams.py）。

重点覆盖"防重启失联"语义：record 同步写 kv（stream_map），新实例 restore 后
映射完整恢复；以及空参数幂等、clear 清空等边界。

运行（项目根）：

    .venv/Scripts/python.exe -m pytest plugins/glcoge-mai-narrative/pytests/test_streams.py -q
    .venv/Scripts/python.exe plugins/glcoge-mai-narrative/pytests/test_streams.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path

import _synth_loader

_STORE_MOD = _synth_loader.load("services.store")
_STREAMS_MOD = _synth_loader.load("services.streams")

NarrativeStore = _STORE_MOD.NarrativeStore
StreamRegistry = _STREAMS_MOD.StreamRegistry

_LOGGER = logging.getLogger("streams-test")


def _make_registry(data_dir: Path):
    """构造真实 store + 注册表。"""
    return StreamRegistry(NarrativeStore(data_dir), _LOGGER)


def test_record_persists_and_restores_after_restart():
    """record 写 kv 持久化；"重启"（新 store/registry 实例）restore 后映射完整。"""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        registry = _make_registry(data_dir)
        registry.record("u1", "s1")
        registry.record("u2", "s2")

        restarted = _make_registry(data_dir)
        restarted.restore()

        assert restarted.stream_of("u1") == "s1"
        assert restarted.stream_of("u2") == "s2"
        assert restarted.uid_of("s1") == "u1"
        assert restarted.uid_of("s2") == "u2"
        assert restarted.known_count() == 2


def test_record_overwrites_existing_mapping():
    """同一用户学到新 stream_id → 正映射指向新值。

    注意：反向映射保留旧条目（s-old 仍反查到 u1）——与 plugin.py 原实现一致
    （纯搬移不改行为）；旧流本就属于该用户，反查命中无害。
    """
    with tempfile.TemporaryDirectory() as tmp:
        registry = _make_registry(Path(tmp))
        registry.record("u1", "s-old")
        registry.record("u1", "s-new")

        assert registry.stream_of("u1") == "s-new"
        assert registry.uid_of("s-new") == "u1"
        assert registry.uid_of("s-old") == "u1"


def test_record_ignores_empty_inputs():
    """空 uid / 空 stream_id 直接忽略（不写脏数据）。"""
    with tempfile.TemporaryDirectory() as tmp:
        registry = _make_registry(Path(tmp))
        registry.record("", "s1")
        registry.record("u1", "")

        assert registry.known_count() == 0


def test_clear_empties_both_maps():
    """clear 清空正反映射（状态重置用）。"""
    with tempfile.TemporaryDirectory() as tmp:
        registry = _make_registry(Path(tmp))
        registry.record("u1", "s1")
        registry.clear()

        assert registry.stream_of("u1") == ""
        assert registry.uid_of("s1") == ""
        assert registry.known_count() == 0


def test_unknown_lookups_return_empty_string():
    """未登记的用户/会话反查返回空串（调用方据此跳过）。"""
    with tempfile.TemporaryDirectory() as tmp:
        registry = _make_registry(Path(tmp))
        assert registry.stream_of("nobody") == ""
        assert registry.uid_of("no-stream") == ""
        assert registry.known_count() == 0


# ===== 独立运行入口 =====

if __name__ == "__main__":
    sys.exit(_synth_loader.run_standalone(globals()))
