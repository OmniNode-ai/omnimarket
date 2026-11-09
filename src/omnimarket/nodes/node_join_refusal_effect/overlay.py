# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_join_refusal_effect: every setting of the join answers, none in code.

Found at ``<root>/node_join_refusal_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package
ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import ModelPieceEventRule
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

NODE_NAME = "node_join_refusal_effect"


class ModelRefusalEmission(BaseModel):
    """The emission journal the floor reads its receipts from, relative to a directory an environment variable names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)
    #: How far back the journal is read to see which failures the downstream piece has already answered: the scope of the
    #: once-only answer.
    window_hours: int = Field(ge=1)


class ModelRefusedJoin(BaseModel):
    """One join this node answers for the downstream piece.

    ``upstream`` is the floor overlay's piece event rule of the upstream piece, copied as it stands there, so the failure
    chains this node answers are the chains the floor builds from the same ledger rows, under the same correlation ids.
    ``downstream_node`` is the node name the typed refusal is emitted under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    join: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    downstream_node: str = Field(min_length=1)
    upstream: ModelPieceEventRule
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelRefusedJoin:
        if self.from_piece == self.to_piece:
            raise ValueError("a join joins two pieces")
        if self.upstream.piece != self.from_piece:
            raise ValueError("the upstream rule is for the join's from_piece")
        return self


class ModelJoinRefusalOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute = Field(
        discriminator="route"
    )
    emission: ModelRefusalEmission
    joins: tuple[ModelRefusedJoin, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelJoinRefusalOverlay:
        ids = [j.join for j in self.joins]
        if len(ids) != len(set(ids)):
            raise ValueError("a join is declared twice")
        return self


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinRefusalOverlay:
    return load_structured_overlay(NODE_NAME, ModelJoinRefusalOverlay, environ)
