# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and policy models for delegation admission (OMN-21097)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class EnumAdmissionReason(StrEnum):
    """Why an Agent call was or was not admitted to delegation."""

    DELEGATABLE = "delegatable"
    NOT_DELEGATABLE = "not_delegatable"
    SENSITIVE = "sensitive"
    UNSUPPORTED_PATH = "unsupported_path"
    UNKNOWN_CALL_ORIGIN = "unknown_call_origin"
    GUARD_NO_MODEL = "guard_no_model"
    GUARD_MODEL_NOT_ALLOWED = "guard_model_not_allowed"
    GUARD_ROUTE_MISSING = "guard_route_missing"
    GUARD_ROUTE_UNPARSABLE = "guard_route_unparsable"
    GUARD_ROUTE_SCORE_MISMATCH = "guard_route_score_mismatch"
    GUARD_ROUTE_MODEL_MISMATCH = "guard_route_model_mismatch"
    GUARD_ROUTE_HARD_FLOOR = "guard_route_hard_floor"
    GUARD_ROUTE_BELOW_SCORE = "guard_route_below_score"
    GUARD_ROUTE_BAND_NO_AGENT = "guard_route_band_no_agent"
    GUARD_ROUTE_BAND_MODEL = "guard_route_band_model"
    GUARD_ROUTE_BAND_EFFORT = "guard_route_band_effort"


class EnumEffectiveMode(StrEnum):
    """Client-call mode, ordered observe < selective < required."""

    OBSERVE = "observe"
    SELECTIVE = "selective"
    REQUIRED = "required"


class ModelCallOrigin(BaseModel):
    """The binding's ``next.origin`` for the Agent call (probe row P11d)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin: str
    tier: str


class ModelRouteBand(BaseModel):
    """One band of the agent-model guard's routing contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    score_min: int | None
    score_max: int | None
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    destination: str


class ModelRouteHardFloor(BaseModel):
    """A dimension value that needs at least ``min_band``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str
    value: int
    min_band: str


class ModelDelegationAdmissionPolicy(BaseModel):
    """Admission policy, read from this node's contract ``admission_policy``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1)
    allowed_models: tuple[str, ...] = Field(min_length=1)
    model_raised_origin: ModelCallOrigin
    route_required: bool
    route_bands: dict[str, ModelRouteBand]
    route_hard_floors: tuple[ModelRouteHardFloor, ...]


class ModelDelegationAdmissionRequest(BaseModel):
    """One Agent call, as the binding saw it, after the client-call claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    tool_input: dict[str, JsonValue]
    call_origin: ModelCallOrigin | None = None
    effective_mode: EnumEffectiveMode


class ModelDelegationAdmissionResult(BaseModel):
    """Admission verdict. Carries no prompt text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    admitted: bool
    task_type: str
    reason: EnumAdmissionReason
    reason_details: tuple[str, ...] = ()
    effective_mode: EnumEffectiveMode
    policy_version: str


__all__ = [
    "EnumAdmissionReason",
    "EnumEffectiveMode",
    "ModelCallOrigin",
    "ModelDelegationAdmissionPolicy",
    "ModelDelegationAdmissionRequest",
    "ModelDelegationAdmissionResult",
    "ModelRouteBand",
    "ModelRouteHardFloor",
]
