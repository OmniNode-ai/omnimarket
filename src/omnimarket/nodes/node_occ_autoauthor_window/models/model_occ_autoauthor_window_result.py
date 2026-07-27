# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelOccAutoauthorWindowResult — the N-window counter output (OMN-14393).

The trailing consecutive-clean streak over the observation trail and whether it
has reached N. This is the evidence gate for the future OMN-14393 fail-closed
flip; it never itself flips anything.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOccAutoauthorWindowResult(BaseModel):
    """The window counter's verdict over the observation trail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consecutive_clean: int = Field(
        ...,
        ge=0,
        description="Length of the trailing run of clean machine-minted observations.",
    )
    required_streak: int = Field(
        ..., ge=1, description="N: the streak threshold the trail is measured against."
    )
    flip_ready: bool = Field(
        ...,
        description=(
            "True iff consecutive_clean >= required_streak AND the composition "
            "floor is met over tuple-keyed records (OMN-14954). Evidence for "
            "the future flip — NOT the flip itself. Never True from legacy "
            "bare observations (composition unverifiable, fail-closed)."
        ),
    )
    total_observations: int = Field(
        ..., ge=0, description="Total observations/raw records in the trail."
    )
    distinct_tuples: int = Field(
        default=0,
        ge=0,
        description=(
            "Distinct exact source tuples after dedup (record mode only; 0 in "
            "legacy observations mode where tuple identity is unknown)."
        ),
    )
    merged_path_clean: int = Field(
        default=0,
        ge=0,
        description="Merged-path records inside the trailing clean streak.",
    )
    runtime_gated_clean: int = Field(
        default=0,
        ge=0,
        description="Runtime/deploy-gated records inside the trailing clean streak.",
    )
    composition_met: bool = Field(
        default=False,
        description=(
            "True iff merged_path_clean >= min_merged_path AND "
            "runtime_gated_clean >= min_runtime_gated, measured over the "
            "trailing clean streak of distinct tuples. Always False in legacy "
            "observations mode (unverifiable — fail-closed)."
        ),
    )
    streak_broken_by: str = Field(
        default="",
        description="repo#pr of the most recent non-clean observation preceding the trailing streak, or '' if none.",
    )
    summary: str = Field(
        default="", description="Human-readable one-line summary of the window state."
    )


__all__ = ["ModelOccAutoauthorWindowResult"]
