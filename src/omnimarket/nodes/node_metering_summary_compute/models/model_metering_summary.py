# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local metering and savings models (OMN-18697).

The shapes here encode three refusals, and each one is the reason a field
exists rather than a convenience on top of it.

**A savings figure never travels without its baseline.** ``savings_usd`` is
``None`` unless ``baseline`` is populated, and ``baseline`` carries the model
whose price was pinned, both per-1k prices, the manifest ``as_of`` and the
manifest version. A saving is a subtraction, and a subtraction whose subtrahend
cannot be named is a number, not a measurement. This is AC2 of OMN-18697, and
the model is what makes the falsifier impossible rather than merely unlikely.

**An unmeasured run is UNKNOWN, never zero.** The local ``delegation_events``
table declares ``cost_savings_usd NOT NULL DEFAULT 0.0``, so a run that
recorded nothing and a run that genuinely saved nothing are the same bytes on
disk. :class:`EnumMeteringMeasurement` splits them, and the summary carries a
count per class, so a reader can always see how much of the window the headline
figure actually rests on. Summing a column would have silently answered with
the wrong denominator. This is AC3.

**The writer's own savings column is quarantined.** Rows on this machine carry
``cost_savings_usd`` but no ``premium_counterfactual``, so the baseline behind
that column is not readable from the record. It is preserved for reconciliation
against earlier hand queries under :class:`ModelMeteringReconciliation`, which
states in its own field names that the baseline is absent. It is deliberately
not the headline number.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumMeteringMeasurement(StrEnum):
    """How completely one delegation run was measured.

    The classes are ordered by how much of the savings subtraction they
    support. Only :attr:`MEASURED` supports all of it.
    """

    #: Token counts AND a spend fact were both recorded. Both halves of
    #: ``counterfactual - spend`` are available, so this run contributes to the
    #: headline savings figure.
    MEASURED = "measured"
    #: No token counts were recorded, so no counterfactual can be built at any
    #: baseline. Nothing about this run's cost is knowable from the record.
    UNKNOWN_TOKENS = "unknown_tokens"
    #: Token counts were recorded but no spend was. We know what the work would
    #: have cost at the baseline; we do not know what it did cost, so the
    #: subtraction has no subtrahend and the run is excluded from savings.
    UNKNOWN_SPEND = "unknown_spend"


class EnumBaselineState(StrEnum):
    """Whether the counterfactual baseline could be pinned for this summary."""

    #: The baseline model was found in the pricing manifest and its prices,
    #: effective date and manifest version are carried on the summary.
    RESOLVED = "resolved"
    #: The baseline model is absent from the pricing manifest. No savings
    #: figure is emitted at all — not a zero, not an estimate.
    UNRESOLVED = "unresolved"


class ModelCounterfactualBaseline(BaseModel):
    """The pinned "what would this have cost instead" price, with provenance.

    Every field is provenance. ``counterfactual_usd`` on the summary always
    equals ``price_in_per_1k * tokens_in/1000 + price_out_per_1k *
    tokens_out/1000`` over the measured rows, so a reader can recompute the
    headline from the printed baseline without access to the database.

    Prices are read from the canonical pricing manifest by the caller and
    passed in. Nothing in this package hardcodes a price.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(
        ...,
        min_length=1,
        description="Baseline model whose price was pinned, e.g. 'claude-opus-4-6'.",
    )
    price_in_per_1k: Decimal = Field(
        ..., ge=Decimal("0"), description="Input-token price, USD per 1,000 tokens."
    )
    price_out_per_1k: Decimal = Field(
        ..., ge=Decimal("0"), description="Output-token price, USD per 1,000 tokens."
    )
    as_of: str = Field(
        ...,
        min_length=10,
        max_length=10,
        description=(
            "ISO-8601 date (YYYY-MM-DD) the pinned price was effective, carried "
            "verbatim from the pricing manifest entry's effective_date."
        ),
    )
    pricing_manifest_version: str = Field(
        ...,
        min_length=1,
        description="schema_version of the pricing manifest the prices came from.",
    )
    source: str = Field(
        ...,
        min_length=1,
        description=(
            "Where the prices came from, e.g. 'pricing_manifest'. Never a "
            "placeholder — an unresolvable baseline is absent, not defaulted."
        ),
    )


class ModelMeteringRecord(BaseModel):
    """One delegation run as the metering reader saw it on disk.

    ``None`` means "not recorded" throughout, and is never interchangeable with
    zero. The reader is responsible for turning the storage layer's ambiguous
    defaults into an honest ``None`` before a record reaches this node; see
    :mod:`omnimarket.projection.sqlite_metering_reader`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(..., min_length=1)
    occurred_at: datetime = Field(
        ..., description="When the run happened, normalised to an aware UTC instant."
    )
    model: str = Field(
        default="",
        description="Model the work was delegated to. Empty when unrecorded.",
    )
    task_type: str = Field(default="", description="Task class, when recorded.")
    tokens_in: int | None = Field(
        default=None, ge=0, description="Prompt tokens, or None when not recorded."
    )
    tokens_out: int | None = Field(
        default=None, ge=0, description="Completion tokens, or None when not recorded."
    )
    spend_usd: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        description="What this run actually cost, or None when not recorded.",
    )
    recorded_savings_usd: Decimal | None = Field(
        default=None,
        description=(
            "The writer's own cost_savings_usd column. Carried for "
            "reconciliation only; the baseline behind it is not on the record."
        ),
    )

    @property
    def has_tokens(self) -> bool:
        """Whether a counterfactual can be built for this run at all.

        Both counts absent means unrecorded. A run with a recorded zero in one
        direction and a real count in the other is measured, not unknown.
        """
        return self.tokens_in is not None or self.tokens_out is not None

    @property
    def measurement(self) -> EnumMeteringMeasurement:
        """This run's measurement class — the AC3 unknown/zero distinction."""
        if not self.has_tokens:
            return EnumMeteringMeasurement.UNKNOWN_TOKENS
        if self.spend_usd is None:
            return EnumMeteringMeasurement.UNKNOWN_SPEND
        return EnumMeteringMeasurement.MEASURED


class ModelMeteringWindow(BaseModel):
    """The half-open window a summary covers: ``[start, end)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(
        ..., min_length=1, description="Human name for the window, e.g. '7d'."
    )
    start: datetime | None = Field(
        default=None,
        description="Inclusive start, or None for an all-time window.",
    )
    end: datetime = Field(..., description="Exclusive end, and the 'now' of the run.")


class ModelMeteringModelRow(BaseModel):
    """Per-model breakdown inside one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    runs: int = Field(..., ge=0)
    runs_measured: int = Field(..., ge=0)
    tokens_in: int = Field(..., ge=0)
    tokens_out: int = Field(..., ge=0)
    spend_usd: Decimal | None = Field(
        default=None, description="Summed over measured runs; None when there are none."
    )
    savings_usd: Decimal | None = Field(
        default=None,
        description="Summed over measured runs at the summary's baseline; None when there are none.",
    )


class ModelMeteringReconciliation(BaseModel):
    """The writer's own savings column, quarantined from the headline.

    This exists so a figure produced by an earlier hand query over
    ``SUM(cost_savings_usd)`` can be reconciled against this projection without
    that column being mistaken for a baselined measurement. Its own field names
    say the baseline is missing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recorded_savings_usd: Decimal = Field(
        ...,
        description=(
            "Sum of the writer's cost_savings_usd over every run in the window, "
            "including runs this projection classes as unknown."
        ),
    )
    recorded_savings_runs: int = Field(
        ..., ge=0, description="Runs that carried a non-null cost_savings_usd."
    )
    recorded_savings_baseline_available: bool = Field(
        default=False,
        description=(
            "Whether the baseline behind recorded_savings_usd is readable from "
            "the records. False on every local row today: premium_counterfactual "
            "is not persisted by the local SQLite projection path."
        ),
    )


class ModelMeteringSummaryRequest(BaseModel):
    """Input to the pure summary node.

    The caller performs every read — the records and the baseline both arrive
    already resolved — so the arithmetic and the unknown-classification rules
    can be tested against a window that has already happened, with no database
    and no pricing manifest on disk.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: ModelMeteringWindow
    records: tuple[ModelMeteringRecord, ...] = Field(default=())
    baseline: ModelCounterfactualBaseline | None = Field(
        default=None,
        description=(
            "The pinned counterfactual price. None when the caller could not "
            "resolve the baseline model from the pricing manifest, which makes "
            "the savings figure UNKNOWN rather than zero."
        ),
    )
    top_models: int = Field(
        default=10, ge=0, description="How many models to break out, by run count."
    )


class ModelMeteringSummary(BaseModel):
    """What a local install can say about its own metering and savings.

    Read the three run counts together with the headline: ``savings_usd`` is a
    statement about ``runs_measured`` runs, not about ``runs_total``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: ModelMeteringWindow
    runs_total: int = Field(..., ge=0)
    runs_measured: int = Field(..., ge=0)
    runs_unknown_tokens: int = Field(..., ge=0)
    runs_unknown_spend: int = Field(..., ge=0)

    tokens_in: int = Field(
        ..., ge=0, description="Summed over runs that recorded token counts."
    )
    tokens_out: int = Field(..., ge=0)

    spend_usd: Decimal | None = Field(
        default=None,
        description="Actual spend over measured runs. None when nothing was measured.",
    )
    counterfactual_usd: Decimal | None = Field(
        default=None,
        description=(
            "What the measured runs' tokens would have cost at the baseline. "
            "None when there is no baseline or no measured run."
        ),
    )
    savings_usd: Decimal | None = Field(
        default=None,
        description=(
            "counterfactual_usd - spend_usd. None — never zero — when the "
            "baseline is unresolved or no run was fully measured."
        ),
    )

    baseline_state: EnumBaselineState
    baseline: ModelCounterfactualBaseline | None = Field(default=None)

    reconciliation: ModelMeteringReconciliation
    by_model: tuple[ModelMeteringModelRow, ...] = Field(default=())

    @model_validator(mode="after")
    def _savings_never_travels_without_its_baseline(self) -> ModelMeteringSummary:
        """AC2, enforced at the model rather than trusted to the handler.

        A summary carrying a savings figure and no baseline cannot be
        constructed, so no code path — including a future one — can emit the
        bare number this ticket's falsifier describes.
        """
        if self.savings_usd is not None and self.baseline is None:
            raise ValueError(
                "savings_usd is set but baseline is None: a savings figure must "
                "carry the counterfactual baseline it was derived against "
                "(OMN-18697 AC2)"
            )
        if self.counterfactual_usd is not None and self.baseline is None:
            raise ValueError(
                "counterfactual_usd is set but baseline is None: the pinned "
                "price it was computed from must be readable beside it"
            )
        if (self.baseline is None) != (
            self.baseline_state is EnumBaselineState.UNRESOLVED
        ):
            raise ValueError(
                "baseline_state must be UNRESOLVED exactly when baseline is None"
            )
        counted = (
            self.runs_measured + self.runs_unknown_tokens + self.runs_unknown_spend
        )
        if counted != self.runs_total:
            raise ValueError(
                f"run classes sum to {counted} but runs_total is {self.runs_total}: "
                "every run must land in exactly one measurement class (OMN-18697 AC3)"
            )
        return self
