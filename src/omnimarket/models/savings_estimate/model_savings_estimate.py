# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prompt records and estimated savings rows (OMN-19979).

Two refusals shape these models.

**An estimate is never a measurement.** A transcript tells us what a prompt
cost at the model that answered it; it cannot tell us what the delegated route
would have cost, because that route never ran. So every
:class:`ModelSavingsEstimateRow` carries ``basis = estimated`` as a literal it
cannot be constructed without, and every money field is named ``estimated_*``.
The measured metering fold takes ``ModelMeteringRecord`` (extra fields
forbidden), so an estimate row is refused at the boundary of every measured
sum rather than trusted to stay out of it.

**No prompt text leaves the reader.** A prompt record carries the counts the
estimate needs (tokens, the answering model, the prompt's length) and never
the text itself.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from omnibase_core.models.delegation.wire import ModelTierCost
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_metering_summary_compute import EnumBaselineState


class EnumSavingsBasis(StrEnum):
    """What a savings figure rests on."""

    #: A delegated run that happened, with recorded tokens and spend.
    MEASURED = "measured"
    #: A counterfactual over historical prompts that were never delegated.
    ESTIMATED = "estimated"


class ModelClaudeCodePromptRecord(BaseModel):
    """One user prompt from a Claude Code session, with the usage it caused.

    Usage is summed over the distinct assistant messages between this prompt
    and the next one in the same session. Claude Code writes one transcript
    line per content block and repeats the message's usage on each, so the
    reader counts each assistant message id once.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(..., min_length=1)
    prompt_id: str = Field(
        ...,
        min_length=1,
        description="The transcript line's uuid, or '<session_id>:<n>' without one.",
    )
    project: str = Field(
        default="", description="The session file's project directory."
    )
    occurred_at: AwareDatetime
    model: str = Field(
        default="",
        description="The first model that answered the prompt; empty when none did.",
    )
    tokens_in: int = Field(default=0, ge=0, description="Uncached input tokens.")
    tokens_out: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(
        default=0,
        ge=0,
        description="Carried for audit; the manifest has no cache rate.",
    )
    cache_read_tokens: int = Field(
        default=0,
        ge=0,
        description="Carried for audit; the manifest has no cache rate.",
    )
    assistant_messages: int = Field(default=0, ge=0)
    prompt_chars: int = Field(default=0, ge=0)

    @property
    def has_usage(self) -> bool:
        """Whether a model answered with recorded usage."""
        return bool(self.model) and (self.tokens_in > 0 or self.tokens_out > 0)


class ModelEstimateDelegateRoute(BaseModel):
    """The route a prompt would have taken through the delegate, pinned.

    The caller reads it from the routing tier registry the delegate uses; the
    compute node prices tokens through ``cost`` and nothing else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier_name: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    cost: ModelTierCost


# The CSV form of a row, in order. The shim that writes the file renders
# ModelSavingsEstimateRow.csv_row() under this header and adds nothing.
ESTIMATE_CSV_COLUMNS: tuple[str, ...] = (
    "basis",
    "session_id",
    "prompt_id",
    "project",
    "occurred_at",
    "source_model",
    "tokens_in",
    "tokens_out",
    "baseline_model",
    "baseline_state",
    "pricing_manifest_version",
    "estimated_baseline_cost_usd",
    "delegate_tier",
    "delegate_model",
    "estimated_delegate_cost_usd",
    "estimated_savings_usd",
)


class ModelSavingsEstimateRow(BaseModel):
    """One historical prompt's estimated saving, had it been delegated.

    ``estimated_savings_usd`` is the baseline counterfactual minus the delegate
    route's cost. It is ``None`` exactly when the baseline is unresolved: no
    baseline price, no figure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: Literal[EnumSavingsBasis.ESTIMATED] = EnumSavingsBasis.ESTIMATED
    session_id: str = Field(..., min_length=1)
    prompt_id: str = Field(..., min_length=1)
    project: str = ""
    occurred_at: AwareDatetime
    source_model: str = Field(..., min_length=1)
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    baseline_model: str = Field(..., min_length=1)
    baseline_state: EnumBaselineState
    pricing_manifest_version: str | None = None
    estimated_baseline_cost_usd: Decimal | None = None
    delegate_tier: str = Field(..., min_length=1)
    delegate_model: str = Field(..., min_length=1)
    estimated_delegate_cost_usd: Decimal = Field(..., ge=Decimal("0"))
    estimated_savings_usd: Decimal | None = None

    @model_validator(mode="after")
    def _no_baseline_no_figure(self) -> ModelSavingsEstimateRow:
        resolved = self.baseline_state is EnumBaselineState.RESOLVED
        if resolved != (self.estimated_baseline_cost_usd is not None):
            raise ValueError(
                "estimated_baseline_cost_usd must be set exactly when the "
                "baseline is resolved"
            )
        if resolved != (self.estimated_savings_usd is not None):
            raise ValueError(
                "estimated_savings_usd must be set exactly when the baseline "
                "is resolved: an unresolved baseline is no figure, not zero"
            )
        if resolved and self.pricing_manifest_version is None:
            raise ValueError("a resolved baseline must name its manifest version")
        return self

    def csv_row(self) -> tuple[str, ...]:
        """The row under :data:`ESTIMATE_CSV_COLUMNS`; unknown money is empty."""
        values = self.model_dump(mode="json")
        return tuple(
            "" if values[column] is None else str(values[column])
            for column in ESTIMATE_CSV_COLUMNS
        )
