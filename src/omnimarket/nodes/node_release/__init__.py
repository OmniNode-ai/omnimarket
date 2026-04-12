"""node_release — Org-wide coordinated release pipeline."""

from omnimarket.nodes.node_release.handlers.handler_release import HandlerRelease
from omnimarket.nodes.node_release.models.model_release_state import (
    ModelReleaseCompletedEvent,
    ModelReleaseStartCommand,
)

__all__ = [
    "HandlerRelease",
    "ModelReleaseCompletedEvent",
    "ModelReleaseStartCommand",
    "NodeRelease",
]


class NodeRelease(HandlerRelease):
    """ONEX entry-point wrapper for HandlerRelease."""
