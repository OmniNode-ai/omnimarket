# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport faults a synthetic probe names, keyed by model id (OMN-23162).

A probe ladder in ``rungs`` mode replaces the routing contract's ladder with
rungs it names. The ``faults`` mode leaves the ladder alone: the production
order is resolved as usual and the file only says that a call to a given model
id answers a given transport fault instead of reaching the provider. What the
run does next, the retry, the sibling hop, the escalation, is production's own
decision, so the order the run walks is the order under test.

Only synthetic traffic whose source surface the probe-ladder admission rule
names may carry one; the port refuses it on any other request before any
provider is called. This module declares new classes only: the released
``ModelProbeLadder`` is untouched, so a consumer pinned to an earlier release
decodes every ``rungs`` ladder as before.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass

# Transport statuses a probe may force, and the failure class each one types as.
# 429 is the provider refusing the call for rate; the 5xx statuses are the
# provider or its gateway being unavailable.
PROBE_FAULT_FAILURE_CLASSES: dict[int, EnumDelegationFailureClass] = {
    429: EnumDelegationFailureClass.RATE_LIMITED,
    500: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    502: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    503: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
    504: EnumDelegationFailureClass.MODEL_UNAVAILABLE,
}


class ModelProbeFault(BaseModel):
    """One model id and the transport status every call to it answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str = Field(..., min_length=1)
    status_code: int = Field(
        default=429,
        description="HTTP status the call answers instead of reaching the provider.",
    )

    @field_validator("status_code")
    @classmethod
    def validate_status_code(cls, value: int) -> int:
        """Refuse a status the port does not type as a transport fault."""
        if value not in PROBE_FAULT_FAILURE_CLASSES:
            raise ValueError(
                f"probe fault status must be one of "
                f"{sorted(PROBE_FAULT_FAILURE_CLASSES)}, got {value}"
            )
        return value

    @property
    def failure_class(self) -> EnumDelegationFailureClass:
        """The failure class this fault's status types as."""
        return PROBE_FAULT_FAILURE_CLASSES[self.status_code]


class ModelProbeFaults(BaseModel):
    """The faults one synthetic probe run injects into the production ladder."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["faults"] = "faults"
    faults: tuple[ModelProbeFault, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_distinct_models(self) -> Self:
        """A model id answers one fault, so none appears twice."""
        model_ids = [fault.model_id for fault in self.faults]
        if len(set(model_ids)) != len(model_ids):
            raise ValueError(f"probe faults repeat a model id: {model_ids}")
        return self

    def fault_for(self, model_id: str) -> ModelProbeFault | None:
        """The fault named for ``model_id``, or ``None`` when it is not faulted."""
        for fault in self.faults:
            if fault.model_id == model_id:
                return fault
        return None


__all__ = [
    "PROBE_FAULT_FAILURE_CLASSES",
    "ModelProbeFault",
    "ModelProbeFaults",
]
