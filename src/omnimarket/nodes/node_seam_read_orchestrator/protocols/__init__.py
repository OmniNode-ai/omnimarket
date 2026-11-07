# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of the attach-on-demand activation: bring one on-demand node onto the bus, and take it off."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_seam_read_orchestrator.models.model_attach_on_demand import (
    ModelAttachOnDemandRequest,
)


class ModelAttachOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attached: bool
    #: A cell-safe token naming why a node was not attached; empty when it was.
    detail: str = ""


class ProtocolNodeAttacher(Protocol):
    """Attach the node that consumes a request's command topic, and detach it again.

    attach never raises: a node that is absent, not loaded or refused is attached=False
    with the reason. detach of a node that is not attached does nothing.
    """

    def attach(self, request: ModelAttachOnDemandRequest) -> ModelAttachOutcome: ...

    def detach(self, request: ModelAttachOnDemandRequest) -> None: ...


__all__ = ["ModelAttachOutcome", "ProtocolNodeAttacher"]
