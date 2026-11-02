# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed blocks the estimate reads from its contract (OMN-22126).

``config.csv_import_schema`` is the one declaration of the CSV upload shape: the
parser reads it and so does the page that tells a visitor what to upload, so a
file one accepts the other accepts. ``config.saving_range`` turns each pricing
manifest confidence level and each token method into the spread of the saving
range. Neither block is restated in code.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import get_args

from omnibase_infra.models.pricing.model_pricing_entry import PricingConfidence
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ZERO = Decimal("0")
_ONE = Decimal("1")


class EnumTokenMethod(StrEnum):
    """How a row's token counts were obtained."""

    REPORTED = "reported"
    TEXT_LENGTH_INPUT = "text_length_input"
    TEXT_LENGTH_OUTPUT = "text_length_output"
    TEXT_LENGTH_BOTH = "text_length_both"


def _aliases(value: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        raise ValueError("a column needs at least one accepted name")
    if any(name != name.strip().lower() or not name for name in value):
        raise ValueError("column names are lower case with no surrounding space")
    if len(set(value)) != len(value):
        raise ValueError("a column lists an accepted name twice")
    return value


class ModelCsvRequiredColumns(BaseModel):
    """Columns a CSV upload must carry; each lists its accepted names in precedence order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: tuple[str, ...]

    @field_validator("*")
    @classmethod
    def _accepted_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _aliases(value)


class ModelCsvOptionalColumns(BaseModel):
    """Columns a CSV upload may carry; each lists its accepted names in precedence order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: tuple[str, ...]
    conversation_id: tuple[str, ...]
    timestamp: tuple[str, ...]
    response: tuple[str, ...]
    tokens_in: tuple[str, ...]
    tokens_out: tuple[str, ...]

    @field_validator("*")
    @classmethod
    def _accepted_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _aliases(value)


class ModelCsvImportSchema(BaseModel):
    """The CSV upload shape: the parser's only source of column names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(..., min_length=1)
    required_columns: ModelCsvRequiredColumns
    optional_columns: ModelCsvOptionalColumns

    @model_validator(mode="after")
    def _no_name_serves_two_columns(self) -> ModelCsvImportSchema:
        names = [
            name
            for block in (self.required_columns, self.optional_columns)
            for aliases in block.model_dump().values()
            for name in aliases
        ]
        if len(set(names)) != len(names):
            raise ValueError("an accepted name belongs to more than one column")
        return self

    def accepted_names(self) -> frozenset[str]:
        """Every column name the schema accepts, under any column."""
        return frozenset(
            name
            for block in (self.required_columns, self.optional_columns)
            for aliases in block.model_dump().values()
            for name in aliases
        )


class ModelSavingRangeSpread(BaseModel):
    """Fractional half-widths that widen the saving into a range.

    ``confidence`` carries one entry per confidence level the pricing manifest
    model declares and scales each model's price; ``token_method`` carries one
    entry per token method and scales the saving for counts not read from the
    export. Each spread is in ``[0, 1)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    confidence: dict[str, Decimal]
    token_method: dict[EnumTokenMethod, Decimal]

    @model_validator(mode="after")
    def _one_entry_per_level(self) -> ModelSavingRangeSpread:
        levels = set(get_args(PricingConfidence))
        if set(self.confidence) != levels:
            raise ValueError(
                f"confidence must name exactly the manifest levels {sorted(levels)}"
            )
        if set(self.token_method) != set(EnumTokenMethod):
            raise ValueError("token_method must name every token method")
        for spread in (*self.confidence.values(), *self.token_method.values()):
            if not _ZERO <= spread < _ONE:
                raise ValueError("every spread is a fraction in [0, 1)")
        return self


class ModelPromptCostEstimateTopics(BaseModel):
    """The contract's command and result topics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(..., min_length=1)
    completed: str = Field(..., min_length=1)


class ModelPromptCostEstimateConfig(BaseModel):
    """Everything the estimate reads from its contract, with the overlay applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    csv_import_schema: ModelCsvImportSchema
    saving_range: ModelSavingRangeSpread


class ModelPromptCostEstimateOverlay(BaseModel):
    """``<root>/prompt_cost_estimate/overlay.yaml``; every key is optional.

    Only the saving range may be overlaid. The CSV schema is not: the page reads
    it from the contract, and an overlaid schema would make the two disagree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    saving_range: ModelSavingRangeSpread | None = None
