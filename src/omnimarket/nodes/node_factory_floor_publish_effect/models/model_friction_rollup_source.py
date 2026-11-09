# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The friction rollup step of the floor publish: its contract settings and the class-rule overlay (OMN-22478)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.friction_rollup import (
    ModelFrictionClassOwner,
    ModelFrictionClassRule,
)


class ModelFrictionRollupStepSettings(BaseModel):
    """What the rollup step reads and how it windows it; every value is the contract's, none is in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_read_command_env: str = Field(
        min_length=1,
        description="The variable that holds the bus read command; the ledger is read through the bus, never from a file.",
    )
    ledger_read_timeout_seconds: int = Field(ge=1, le=600)
    ledger_row_limit: int = Field(
        ge=1,
        description="Rows one bus read may return; an answer longer than this is refused as truncated, never rolled short.",
    )
    ledger_row_kinds: tuple[str, ...] = Field(
        min_length=1,
        description="The row kinds the rollup reads: the FRICTION rows and the kinds its signals are drawn from.",
    )
    ledger_chunk_hours: int = Field(
        ge=1,
        description="Each bus read covers this many hours, so no answer outgrows its limit.",
    )
    window_hours: int = Field(gt=0)
    bucket_hours: int = Field(gt=0)
    umbrella_tickets: tuple[str, ...]
    class_overlay_id: str = Field(min_length=1)
    class_overlay_file_env: str = Field(min_length=1)


class ModelFrictionClassOverlay(BaseModel):
    """The overlay home's class rules and class owners, the shape of the rollup node's ``overlay.example.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_rules: tuple[ModelFrictionClassRule, ...]
    class_owners: tuple[ModelFrictionClassOwner, ...] = ()
