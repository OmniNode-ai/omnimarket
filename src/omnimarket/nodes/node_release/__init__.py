"""node_release — Org-wide coordinated release pipeline."""

from omnimarket.nodes.node_release.handlers.handler_release import HandlerRelease
from omnimarket.nodes.node_release.models.model_release_state import (
    ModelReleaseCompletedEvent,
    ModelReleaseStartCommand,
)

__all__ = [
    "NodeRelease",
    "HandlerRelease",
    "ModelReleaseCompletedEvent",
    "ModelReleaseStartCommand",
]

class NodeRelease(HandlerRelease):
    """ONEX entry-point wrapper for HandlerRelease."""
