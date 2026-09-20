# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B output for the lab lane-health fold (OMN-18769)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLabLaneHealthResult(BaseModel):
    """The rows one source fact changed, already rendered for the wire.

    ``rows`` is empty for an event that named no lab lane, and that is a
    SUCCESS, not a failure: a runtime on a lane this projection does not hold
    is correctly ignored. The two states are told apart by ``applied``, so a
    caller never has to infer "did this work" from "did anything change".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool = Field(
        description=(
            "True when the fact was understood and folded. An event naming no "
            "lab lane is applied=True with no rows: correctly ignored, not "
            "failed."
        )
    )
    rows: tuple[dict[str, Any], ...] = Field(
        default=(),
        description=(
            "The exposure rows this fact changed, one per lane it touched. "
            "Only lanes the event actually named appear -- republishing an "
            "untouched lane would stamp a freshness claim nothing observed."
        ),
    )
