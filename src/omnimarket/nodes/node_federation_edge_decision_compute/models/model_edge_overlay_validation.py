# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of the edge overlay's table validation.

The edge overlay names one runtime binding and pins itself to one lane, one
topic and one key binding. Before an edge step is decided, the overlay's
claims are checked against the deployment's own tables: the binding is in the
runtime binding table, the lane is declared, the topic is registered, and so
on. This file carries only the shapes: the request (the overlay and the
tables) and the result (``ok`` and the refusal names). The check that fills
the result is the node's handler.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_federation_edge_decision_compute.models.enum_edge_decision import (
    EnumOverlayRefusal,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_decision import (
    ModelLaneDeclaration,
    ModelRegistryTopicEntry,
)
from omnimarket.nodes.node_federation_edge_decision_compute.models.model_edge_overlay import (
    ModelEdgeOverlayFile,
    ModelRuntimeBinding,
)


class ModelOverlayValidationRequest(BaseModel):
    """The overlay to validate, and the tables its claims are checked against.

    ``bindings`` is the runtime binding table (one entry per runtime ref),
    ``lane_declarations`` the lane table the connection's lane is read
    against, and ``registry_entries`` the topic registry the edge's allowed
    topic is read against. Every table arrives whole in the request; the
    validation reads nothing outside it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelEdgeOverlayFile
    bindings: tuple[ModelRuntimeBinding, ...]
    lane_declarations: tuple[ModelLaneDeclaration, ...]
    registry_entries: tuple[ModelRegistryTopicEntry, ...]


class ModelOverlayValidationResult(BaseModel):
    """The overlay validation's answer: closed, or the refusal names that hold it open.

    ``ok`` is exactly ``refusals`` being empty: a result that carries a
    refusal is not ok, and an ok result carries none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    refusals: tuple[EnumOverlayRefusal, ...] = Field(default=())

    @model_validator(mode="after")
    def _ok_matches_refusals(self) -> ModelOverlayValidationResult:
        if self.ok != (not self.refusals):
            raise ValueError("ok is exactly refusals being empty")
        return self


__all__: list[str] = [
    "ModelOverlayValidationRequest",
    "ModelOverlayValidationResult",
]
