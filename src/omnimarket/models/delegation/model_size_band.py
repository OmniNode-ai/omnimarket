# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measured delegation size bands and named input refusals."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.models.delegation.model_feature_provenance import ModelFeatureProvenance


class EnumSizeBand(StrEnum):
    """Size labels ordered from smallest to largest."""

    S = "S"
    M = "M"
    L = "L"

    @property
    def rank(self) -> int:
        """Return the ordinal used to combine feature bands."""
        return tuple(type(self)).index(self)


class EnumSizeInput(StrEnum):
    """Caller fields refused in their declared order."""

    SIZE_BAND = "size_band"
    BAND = "band"
    INPUT_TOKENS = "input_tokens"
    UNITS = "units"
    STEPS = "steps"


class ModelSizeBandRequest(BaseModel):
    """Text inputs with opaque size fields accepted only to name refusals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_class: str = Field(min_length=1)
    prompt: str
    context_pack: str = ""
    sources: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    caller_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    size_band: JsonValue = None
    band: JsonValue = None
    input_tokens: JsonValue = None
    units: JsonValue = None
    steps: JsonValue = None


class ModelSizeFeature(BaseModel):
    """One measured value with its text rule and contract edges."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: int = Field(ge=0)
    band: EnumSizeBand
    source: ModelFeatureProvenance
    threshold: ModelFeatureProvenance

    @model_validator(mode="after")
    def _validate_provenance(self) -> ModelSizeFeature:
        if self.source.source != "text_measurement":
            raise ValueError("size feature source must be text_measurement")
        if self.threshold.source != "contract":
            raise ValueError("size feature threshold must come from the contract")
        return self


class ModelSizeBand(BaseModel):
    """The largest of the measured token, unit, and step bands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["measured"] = "measured"
    task_class: str
    band: EnumSizeBand
    input_tokens: ModelSizeFeature
    units: ModelSizeFeature
    steps: ModelSizeFeature

    @model_validator(mode="after")
    def _validate_band(self) -> ModelSizeBand:
        largest = max(
            self.input_tokens.band,
            self.units.band,
            self.steps.band,
            key=lambda band: band.rank,
        )
        if self.band != largest:
            raise ValueError("request band must be the largest feature band")
        return self


class ModelSizeBandRefusal(BaseModel):
    """A refusal naming every field that prevents measurement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["refused"] = "refused"
    reason: Literal[
        "size_input_supplied", "task_class_unavailable", "size_thresholds_unavailable"
    ]
    fields: tuple[str, ...] = Field(min_length=1)


ModelSizeBandResult = Annotated[
    ModelSizeBand | ModelSizeBandRefusal, Field(discriminator="status")
]
