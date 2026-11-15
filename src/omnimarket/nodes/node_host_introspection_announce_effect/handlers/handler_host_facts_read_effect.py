# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostFactsReadEffect -- read this host's facts (OMN-24367).

Canonical def-B handler: ``handle(request: ModelHostFactsProbeRequest) -> ModelHostFacts``. An unreadable host
raises :class:`HostFactsUnreadableError` and returns no facts: the announcer turns that into a typed failure,
never into an announcement with a guessed field.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.nodes.node_host_introspection_announce_effect.models.model_host_introspection_announce import (
    ModelHostFactsProbeRequest,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols.local_host_facts_reader import (
    LocalHostFactsReader,
)
from omnimarket.nodes.node_host_introspection_announce_effect.protocols.protocol_host_facts_reader import (
    ProtocolHostFactsReader,
)
from omnimarket.nodes.node_host_introspection_compute.models.model_host_announcement import (
    ModelHostAvailability,
    ModelHostFacts,
    ModelHostServedModel,
)


class HandlerHostFactsReadEffect:
    """EFFECT handler: read this host and return its facts."""

    def __init__(self, reader: ProtocolHostFactsReader | None = None) -> None:
        self._reader = reader or LocalHostFactsReader()

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelHostFactsProbeRequest) -> ModelHostFacts:
        reading = self._reader.read(list(request.software))
        return ModelHostFacts(
            host_name=request.host_name,
            roles=sorted(set(request.roles)),
            cores=reading.cores,
            mem_total_bytes=reading.mem_total_bytes,
            gpus=list(reading.gpus),
            models_served=[
                ModelHostServedModel(name=name)
                for name in sorted(set(request.models_served))
            ],
            runtimes=list(reading.runtimes),
            software=list(reading.software),
            lane_accounts=list(reading.lane_accounts),
            availability=ModelHostAvailability(
                takes_work=request.takes_work, reason=request.availability_reason
            ),
        )


__all__ = ["HandlerHostFactsReadEffect"]
