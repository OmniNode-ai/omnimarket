# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Code-eval trial fields of a context-ROI attempt row (OMN-23628)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models.enum_code_score import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)


class EnumContextRoiRowKind(StrEnum):
    """Kind of row a ModelAttemptReductionRow represents (OMN-23628).

    try      -- one code-eval try with its check verdict and timings.
    control  -- a control row kept beside the tries for comparison.
    """

    TRY = "try"
    CONTROL = "control"


class ModelTrialFields(BaseModel):
    """The fields that describe one code-eval try; ModelAttemptReductionRow inherits them.

    check_verdict / unscorable_source record the contract-score outcome;
    row_kind distinguishes a live try from a control row; try_end records
    how the try ended (the loop's status or a unit timeout); suite_digest
    and size_band identify the evaluated suite and its size class;
    sequence_id / try_ordinal / diff_hash / max_turns / try_timeout_s /
    generation_s / env_sync_s / check_s carry the trial's identity, ordinal,
    diff, and per-phase timing. A row written before these fields were
    added reads their defaults (e.g. check_verdict=None, row_kind=TRY,
    size_band=S, all timings 0.0), so older fixtures still validate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_verdict: EnumCodeScoreVerdict | None = Field(
        default=None,
        description=(
            "Contract-score verdict for this try; None when the try was not "
            "scored. When unscorable_source is set, this must be UNSCORABLE."
        ),
    )
    unscorable_source: EnumCodeScoreSource | None = Field(
        default=None,
        description=(
            "Why this try could not be scored (an infrastructure fault, never "
            "the draft's own); None when the try was scored. Allowed only when "
            "check_verdict is UNSCORABLE."
        ),
    )
    row_kind: EnumContextRoiRowKind = Field(
        default=EnumContextRoiRowKind.TRY,
        description=(
            "Kind of row: 'try' for a live code-eval trial, 'control' for a "
            "baseline comparison row. Defaults to TRY so older rows read as tries."
        ),
    )
    try_end: str = Field(
        default="",
        description=(
            "How the try ended: the loop's terminal status or a unit timeout "
            "label; empty string when not captured."
        ),
    )
    suite_digest: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the evaluated suite; empty string when the "
            "suite was not identified (e.g. off arm or unscorable)."
        ),
    )
    size_band: EnumSizeBand = Field(
        default=EnumSizeBand.S,
        description=(
            "Size band of the task this try ran (S, M or L); defaults to S, the "
            "band every row was filed under before this field."
        ),
    )
    sequence_id: str = Field(
        default="",
        description=(
            "Identifier of the sequence of tries this try belongs to; empty "
            "string when not captured."
        ),
    )
    try_ordinal: int = Field(
        default=0,
        ge=0,
        description="Ordinal of this try within its sequence (0 = unset).",
    )
    diff_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of the diff produced by this try; empty string "
            "when no diff was captured."
        ),
    )
    max_turns: int = Field(
        default=0,
        ge=0,
        description="Maximum turns allowed for this try (0 = not set).",
    )
    try_timeout_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Timeout in seconds applied to this try (0.0 = not set).",
    )
    generation_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds spent in the generation phase (0.0 = not captured).",
    )
    env_sync_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds spent in the environment-sync phase (0.0 = not captured).",
    )
    check_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock seconds spent in the check phase (0.0 = not captured).",
    )

    @model_validator(mode="after")
    def _unscorable_source_needs_unscorable_verdict(self) -> Self:
        if (
            self.unscorable_source is not None
            and self.check_verdict is not EnumCodeScoreVerdict.UNSCORABLE
        ):
            raise ValueError(
                "unscorable_source must be None unless check_verdict is UNSCORABLE"
            )
        return self
