# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the landing placement node."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLandingPlacementRequest(BaseModel):
    """One placement command. ``read_pool`` probes every host of the pool and returns each host's reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["read_pool"] = "read_pool"
    # The process environment the pool is resolved from (``LANDING_POOL_HOSTS``, ``ONEX_LAB_RUN_HOSTS``,
    # ``ONEX_REGISTRY_ROOT``, ``ONEX_SKILL_OVERLAY_ROOTS``); omitted: the process environment.
    environ: dict[str, str] | None = None
    lane: str = "landing-controller"
    placed: dict[str, int] = Field(default_factory=dict)


class ModelPoolHostReading(BaseModel):
    """One host's reading, the fields a pool comparison keys on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    engines: tuple[str, ...]
    cap: int
    slots: int
    placed: int
    verdict: str | None = Field(
        description="The admission refusal, or None when the host is admitted."
    )
    error: str | None = None


class ModelLandingPlacementResult(BaseModel):
    """The outcome of a placement command: ``exit_code`` 0, or 75 when no host table is configured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    message: str
    pool_source: str | None = None
    hosts: tuple[ModelPoolHostReading, ...] = ()
