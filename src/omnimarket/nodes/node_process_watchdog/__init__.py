"""node_process_watchdog — infrastructure process watchdog compute node."""

from omnimarket.nodes.node_process_watchdog.handlers.handler_process_watchdog import (
    CheckTarget,
    HandlerProcessWatchdog,
    InmemoryCheckTarget,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_completed_event import (
    ModelWatchdogCompletedEvent,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_start_command import (
    ModelWatchdogStartCommand,
)
from omnimarket.nodes.node_process_watchdog.models.model_watchdog_state import (
    EnumCheckStatus,
    EnumCheckTarget,
    ModelWatchdogCheckResult,
    ModelWatchdogReport,
)

__all__ = [
    "CheckTarget",
    "EnumCheckStatus",
    "EnumCheckTarget",
    "HandlerProcessWatchdog",
    "InmemoryCheckTarget",
    "ModelWatchdogCheckResult",
    "ModelWatchdogCompletedEvent",
    "ModelWatchdogReport",
    "ModelWatchdogStartCommand",
    "NodeProcessWatchdog",
]


class NodeProcessWatchdog(HandlerProcessWatchdog):
    """ONEX entry-point wrapper for HandlerProcessWatchdog."""
