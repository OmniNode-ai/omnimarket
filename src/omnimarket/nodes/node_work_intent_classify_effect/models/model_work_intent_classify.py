# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, configuration and result models of the work-intent fallback effect (OMN-22263)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class EnumRoutePolicy(StrEnum):
    """The only policy the effect accepts: private transcript content routes to lab models only."""

    LOCAL_ONLY = "local_only"


class EnumBackendClass(StrEnum):
    """Where a route's backend runs. CLOUD is a member so it can be refused by name."""

    LAB = "lab"
    CLOUD = "cloud"


class IntentRouteConfigError(ValueError):
    """The overlay's route configuration is refused at load; the message names the key."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key


class ModelDelegationRoute(BaseModel):
    """The overlay key delegation_route: which route, and where its backend runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route_id: str = Field(min_length=1)
    backend_class: EnumBackendClass
    model_version: str = Field(min_length=1)

    @field_validator("backend_class")
    @classmethod
    def _lab_only(cls, value: EnumBackendClass) -> EnumBackendClass:
        if value is not EnumBackendClass.LAB:
            raise ValueError(
                "a route for private transcript content must run on a lab backend"
            )
        return value


class ModelIntentRouteConfig(BaseModel):
    """The overlay keys delegation_route, route_policy, lab_hosts and timeout_s."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    delegation_route: ModelDelegationRoute
    route_policy: EnumRoutePolicy
    lab_hosts: tuple[str, ...] = Field(min_length=1)
    timeout_s: float = Field(gt=0)


def load_intent_route_config(raw: dict[str, object]) -> ModelIntentRouteConfig:
    """Validate the overlay's route keys; a refusal names the first offending key."""
    try:
        return ModelIntentRouteConfig.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        key = ".".join(str(part) for part in first["loc"])
        raise IntentRouteConfigError(key, first["msg"]) from error


class EnumIntentStatus(StrEnum):
    """NOT_REACHED: a rule had decided. CACHED: the stored result was reused. DECIDED: the route answered."""

    NOT_REACHED = "not_reached"
    CACHED = "cached"
    DECIDED = "decided"
    UNDETERMINED = "undetermined"


class ModelWorkIntentClassifyRequest(BaseModel):
    """One unit's fallback call. The intent text is hidden from repr and is not held by the result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    current_work_type: str = Field(min_length=1)
    undetermined_id: str = Field(min_length=1)
    allowed_work_type_ids: tuple[str, ...] = Field(min_length=1)
    intent_text: str | None = Field(repr=False)
    intent_digest: str | None
    rubric_version: str = Field(min_length=1)
    prompt_template: str = Field(min_length=1)
    cause_no_route: str = Field(min_length=1)
    cause_no_intent_text: str = Field(min_length=1)
    cause_route_invalid: str = Field(min_length=1)
    config: ModelIntentRouteConfig | None


class ModelWorkIntentClassifyResult(BaseModel):
    """The fallback outcome. No prompt text is held."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str
    status: EnumIntentStatus
    work_type_id: str
    cause: str | None
    route_receipt_id: str | None
    route_model_version: str | None
    refused: bool


__all__ = [
    "EnumBackendClass",
    "EnumIntentStatus",
    "EnumRoutePolicy",
    "IntentRouteConfigError",
    "ModelDelegationRoute",
    "ModelIntentRouteConfig",
    "ModelWorkIntentClassifyRequest",
    "ModelWorkIntentClassifyResult",
    "load_intent_route_config",
]
