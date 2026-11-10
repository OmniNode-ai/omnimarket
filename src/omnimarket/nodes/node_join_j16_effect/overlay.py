# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_join_j16_effect: every setting of the J16 join, none in code.

Found at ``<root>/node_join_j16_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package ships
``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.floor_17a import ModelLedgerReadingRule
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

NODE_NAME = "node_join_j16_effect"


class ModelJ16Emission(BaseModel):
    """The emission journal the floor reads its receipts from, and the floor record the join reads, both relative to a
    directory an environment variable names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)
    floor_record_relpath: str = Field(min_length=1)


class ModelJ16Join(BaseModel):
    """The join: the piece whose eval result is read, the piece that draws it, the node names the chains are emitted under,
    and the floor overlay's ledger reading rule for the eval piece, copied as it stands there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)
    reading: ModelLedgerReadingRule
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelJ16Join:
        if self.from_piece == self.to_piece:
            raise ValueError("a join joins two pieces")
        return self


class ModelJoinJ16Overlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute = Field(
        discriminator="route"
    )
    emission: ModelJ16Emission
    join: ModelJ16Join


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinJ16Overlay:
    return load_structured_overlay(NODE_NAME, ModelJoinJ16Overlay, environ)
