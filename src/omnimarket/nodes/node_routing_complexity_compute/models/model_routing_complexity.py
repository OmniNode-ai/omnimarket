# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Trusted routing request, refusal, and per-feature provenance models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.models.delegation.model_feature_provenance import (
    ModelFeatureProvenance as ModelFeatureProvenance,
)
from omnimarket.nodes.node_routing_complexity_compute.models.model_complexity import (
    ModelComplexityClassification,
    ModelComplexityFeatures,
)


class EnumRubricInput(StrEnum):
    """Fields whose presence at the request boundary is a typed refusal."""

    SCORER = "scorer"
    DEPENDENT_REASONING_STEPS = "dependent_reasoning_steps"
    OUTPUT_KIND = "output_kind"
    VERIFICATION = "verification"
    EXECUTION_REQUIRED = "execution_required"


class ModelRoutingRequest(BaseModel):
    """Routing input, with opaque rubric fields accepted only to name refusals.

    Presence, not truthiness or validity, refuses each rubric field. None is
    deliberately representable so a caller cannot bypass refusal with null.
    Unknown fields (including contract paths) are rejected by model validation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    workflow: str = Field(min_length=1)
    scorer: JsonValue = None
    dependent_reasoning_steps: JsonValue = None
    output_kind: JsonValue = None
    verification: JsonValue = None
    execution_required: JsonValue = None


class ModelTrustedComplexityClassification(ModelComplexityClassification):
    """A complete provenance map plus the measured/declared feature split."""

    workflow: str
    provenance: dict[str, ModelFeatureProvenance]

    @model_validator(mode="after")
    def _validate_provenance(self) -> ModelTrustedComplexityClassification:
        names = set(ModelComplexityFeatures.model_fields) - {"measured", "declared"}
        if set(self.provenance) != names:
            raise ValueError("every routing feature must carry trusted provenance")
        measured = {
            name
            for name, origin in self.provenance.items()
            if origin.source == "text_measurement"
        }
        declared = names - measured
        if (
            set(self.features.measured) != measured
            or set(self.features.declared) != declared
        ):
            raise ValueError(
                "feature provenance must match the measured/declared split"
            )
        return self


class ModelRoutingClassification(BaseModel):
    """Accepted routing classification, distinct from diagnostic evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["classified"] = "classified"
    classification: ModelTrustedComplexityClassification


class ModelRoutingRefusal(BaseModel):
    """A named refusal; attached evidence never authorizes routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["refused"] = "refused"
    reason: Literal["rubric_input_supplied", "workflow_contract_unavailable"]
    fields: tuple[str, ...] = Field(min_length=1)
    classification: ModelTrustedComplexityClassification | None = None


class ModelStepMeasurement(BaseModel):
    """Validated text measurement definition from the rubric contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    how: str = Field(min_length=1)
    pattern: str = Field(min_length=1)
    base_steps: int = Field(ge=1)


ModelRoutingResult = Annotated[
    ModelRoutingClassification | ModelRoutingRefusal, Field(discriminator="status")
]
