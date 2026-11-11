# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_proof_judge_use_effect: every setting of the use record, none in code.

Found at ``<root>/node_lab_proof_judge_use_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelLedgerAppendRoute,
)

NODE_NAME = "node_lab_proof_judge_use_effect"


class ModelEventSettings(BaseModel):
    """The event bus topic and type the use record is published under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    event_type: str = Field(min_length=1)


class ModelLedgerSettings(BaseModel):
    """The ledger append route, lane, actor, ticket and kind cell of the row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    append: ModelLedgerAppendRoute
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    kind_cell: str = Field(min_length=1)


class ModelLabProofJudgeUseOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(ge=1)
    event: ModelEventSettings
    ledger: ModelLedgerSettings

    @model_validator(mode="after")
    def _shape(self) -> ModelLabProofJudgeUseOverlay:
        if not self.event.topic.strip():
            raise ValueError("event.topic must not be empty")
        if not self.ledger.lane.strip():
            raise ValueError("ledger.lane must not be empty")
        if not self.ledger.ticket.strip():
            raise ValueError("ledger.ticket must not be empty")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLabProofJudgeUseOverlay:
    return load_structured_overlay(NODE_NAME, ModelLabProofJudgeUseOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelEventSettings",
    "ModelLabProofJudgeUseOverlay",
    "ModelLedgerSettings",
    "load_overlay",
]
