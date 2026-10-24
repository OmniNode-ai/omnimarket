# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A per-run ladder a synthetic probe hands the local delegation port.

The routing contract decides every rung of an ordinary delegation, so nothing
can force a climb on purpose. A probe ladder names the rungs one run walks, in
order, instead. Only synthetic traffic from the doctor's escalation check may
carry one: the port refuses it on any other request before any provider is
called. Every rung must point at a loopback endpoint, so a probe can only reach
the stub servers its own process started.
"""

from __future__ import annotations

from typing import Self
from urllib.parse import urlparse

from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from omnibase_core.models.delegation.wire import ModelDelegationProvenance
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# The one producer surface whose synthetic requests may carry a probe ladder.
PROBE_LADDER_SOURCE_SURFACE = "doctor_escalation_check"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ModelProbeRung(BaseModel):
    """One rung of a probe ladder: a backend id, its model and its endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    endpoint_ref: str = Field(
        ..., min_length=1, description="Chat-completions URL on a loopback host."
    )
    tier: str = Field(
        default="local",
        min_length=1,
        description="Routing tier the rung is priced and retried as.",
    )
    max_tokens: int = Field(default=256, ge=1)
    timeout_ms: int = Field(default=30000, ge=1)

    @field_validator("endpoint_ref")
    @classmethod
    def validate_loopback_endpoint(cls, value: str) -> str:
        """Refuse any endpoint off this machine."""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"probe rung endpoint must be http(s), got {value!r}")
        if parsed.hostname not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"probe rung endpoint must be on a loopback host, got {value!r}"
            )
        return value


class ModelProbeLadder(BaseModel):
    """The rungs one synthetic probe run walks, first to last."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rungs: tuple[ModelProbeRung, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_distinct_backends(self) -> Self:
        """Each rung is its own responder, so no backend id appears twice."""
        backend_ids = [rung.backend_id for rung in self.rungs]
        if len(set(backend_ids)) != len(backend_ids):
            raise ValueError(f"probe ladder repeats a backend id: {backend_ids}")
        return self


def probe_ladder_admitted(provenance: ModelDelegationProvenance | None) -> bool:
    """Whether a request with ``provenance`` may carry a probe ladder."""
    return (
        provenance is not None
        and provenance.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
        and provenance.source_surface == PROBE_LADDER_SOURCE_SURFACE
    )


__all__ = [
    "PROBE_LADDER_SOURCE_SURFACE",
    "ModelProbeLadder",
    "ModelProbeRung",
    "probe_ladder_admitted",
]
