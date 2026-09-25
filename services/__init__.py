"""剧本人设系统服务层。"""

from .state.engine import NarrativeEngine
from .proactive.scheduler import ProactiveScheduler, validate_rules
from .render.planner_block import build_context_block, build_injected_item, is_injected_item
from .store import NarrativeStore
from .streams import StreamRegistry
from .state.snapshot import Telemetry

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