"""剧本人设系统服务层。"""

from .deps import Deps
from .state.engine import NarrativeEngine
from .lorebook import LorebookLoader
from .proactive.scheduler import ProactiveScheduler, validate_rules
from .render.planner_block import (
    build_context_block,
    build_injected_item,
    is_injected_item,
    items_dialogue_text,
)
from .store import NarrativeStore
from .streams import GroupStreamRegistry, StreamRegistry
from .state.snapshot import Telemetry

__all__ = [
    "Deps",
    "NarrativeEngine",
    "NarrativeStore",
    "StreamRegistry",
    "GroupStreamRegistry",
    "ProactiveScheduler",
    "validate_rules",
    "Telemetry",
    "LorebookLoader",
    "build_context_block",
    "build_injected_item",
    "is_injected_item",
    "items_dialogue_text",
]