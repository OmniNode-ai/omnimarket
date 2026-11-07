# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory attacher: on-demand fixture nodes become consumers of the in-memory seam bus."""

from __future__ import annotations

from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
    Responder,
)
from omnimarket.nodes.node_seam_read_orchestrator.models.model_attach_on_demand import (
    ModelAttachOnDemandRequest,
)
from omnimarket.nodes.node_seam_read_orchestrator.protocols import ModelAttachOutcome


class InMemoryNodeAttacher:
    """Attaching registers the node's responder on the bus; detaching removes it.

    ``available`` is the on-demand nodes that could be attached, by command topic.
    ``attach_fails`` makes every attach fail with that token. ``attached`` lists the command
    topics attached now.
    """

    def __init__(
        self, bus: InMemorySeamBus, *, available: dict[str, Responder] | None = None
    ) -> None:
        self._bus = bus
        self._available = dict(available or {})
        self.attach_fails: str | None = None
        self._attached: list[str] = []

    @property
    def attached(self) -> tuple[str, ...]:
        return tuple(self._attached)

    def attach(self, request: ModelAttachOnDemandRequest) -> ModelAttachOutcome:
        if self.attach_fails is not None:
            return ModelAttachOutcome(attached=False, detail=self.attach_fails)
        node = self._available.get(request.command_topic)
        if node is None:
            return ModelAttachOutcome(attached=False, detail="no-node-for-topic")
        self._bus.responders[request.command_topic] = node
        self._attached.append(request.command_topic)
        return ModelAttachOutcome(attached=True)

    def detach(self, request: ModelAttachOnDemandRequest) -> None:
        if request.command_topic in self._attached:
            self._attached.remove(request.command_topic)
            self._bus.responders.pop(request.command_topic, None)


__all__ = ["InMemoryNodeAttacher"]
