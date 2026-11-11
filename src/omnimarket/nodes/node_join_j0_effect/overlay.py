# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_join_j0_effect: every setting of the J0 join, none in code.

Found at ``<root>/node_join_j0_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The package ships
``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_join_j0_effect"


class ModelJ0Emission(BaseModel):
    """The emission journal the floor reads its receipts from, and the floor record the join reads, both relative to a
    directory an environment variable names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)
    floor_record_relpath: str = Field(min_length=1)


class ModelJ0Join(BaseModel):
    """The join: the piece that holds the floor record, the piece that draws it, the node names the chains are emitted
    under, and the fault the control deliberately injects on the record-reading side."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    from_node: str = Field(min_length=1)
    to_node: str = Field(min_length=1)
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelJ0Join:
        if self.from_piece == self.to_piece:
            raise ValueError("a join joins two pieces")
        return self


class ModelJ0Control(BaseModel):
    """The control's deliberate fault: the deliberately corrupt floor record text the record reader must reject."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_text: str = Field(min_length=1)
    injected_fault: str = Field(min_length=1)


class ModelJoinJ0Overlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    emission: ModelJ0Emission
    join: ModelJ0Join
    control: ModelJ0Control


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelJoinJ0Overlay:
    return load_structured_overlay(NODE_NAME, ModelJoinJ0Overlay, environ)
