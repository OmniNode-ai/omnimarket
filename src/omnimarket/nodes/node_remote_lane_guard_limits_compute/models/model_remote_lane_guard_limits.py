# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and verdict of the remote-lane guard limit resolution (OMN-22848, plan task T15 step: lane guard limits)."""

from pydantic import BaseModel, ConfigDict, Field


class ModelGuardBounds(BaseModel):
    """The bounds the host guard enforces, supplied by the caller from its overlay.

    Nothing here has a default: a caller that supplies no bound is refused by validation, so the numbers live in one place (the
    overlay) and never in this node.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    hard_cap_s: int = Field(
        ...,
        gt=0,
        description="Longest runtime a guarded command may be given, grace included.",
    )
    grace_s: int = Field(
        ...,
        ge=0,
        description="Seconds added to the lane's own timeout before the guard's deadline.",
    )
    default_mem_bytes: int = Field(
        ..., gt=0, description="Memory limit when the caller names none."
    )
    mem_floor_bytes: int = Field(
        ..., gt=0, description="Smallest memory limit the guard accepts."
    )
    mem_ceiling_bytes: int = Field(
        ..., gt=0, description="Largest memory limit the guard accepts."
    )
    default_cpu_pct: int = Field(
        ..., gt=0, description="CPU quota percent when the caller names none."
    )
    cpu_floor_pct: int = Field(
        ..., gt=0, description="Smallest CPU quota percent the guard accepts."
    )
    cpu_ceiling_pct: int = Field(
        ..., gt=0, description="Largest CPU quota percent the guard accepts."
    )
    tasks_max: int = Field(
        ..., gt=0, description="Task count limit of the guarded scope."
    )
    kinds: tuple[str, ...] = Field(
        ..., min_length=1, description="The guard kinds the host program knows."
    )


class ModelRemoteLaneGuardLimitsRequest(BaseModel):
    """One lane's ask: its timeout and optional memory and CPU limits, and the bounds that judge them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_s: float = Field(..., description="The lane's own timeout in seconds.")
    mem_max: str | None = Field(
        default=None,
        description="Memory ask as 8G, 512M or a byte count; None takes the default.",
    )
    cpu_quota: int | None = Field(
        default=None, description="CPU quota percent; None or 0 takes the default."
    )
    kind: str = Field(
        default="lane",
        description="The guard kind: how the host program treats the command.",
    )
    bounds: ModelGuardBounds


class ModelGuardLimits(BaseModel):
    """The limits one guarded command runs under; the shape the runner's receipt records under guard.requested."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_s: int
    mem_bytes: int
    cpu_pct: int
    tasks_max: int
    kind: str


class ModelRemoteLaneGuardLimitsVerdict(BaseModel):
    """Either the resolved limits or the refusal text, never both."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    limits: ModelGuardLimits | None
    refusal: str | None
