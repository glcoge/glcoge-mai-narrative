"""剧本人设系统服务层。"""

from .engine import NarrativeEngine
from .proactive import ProactiveScheduler, validate_rules
from .render import build_context_block, build_injected_item, is_injected_item
from .store import NarrativeStore
from .streams import StreamRegistry
from .telemetry import Telemetry

__all__ = [
    "NarrativeEngine",
    "NarrativeStore",
    "StreamRegistry",
    "ProactiveScheduler",
    "validate_rules",
    "Telemetry",
    "build_context_block",
    "build_injected_item",
    "is_injected_item",
]