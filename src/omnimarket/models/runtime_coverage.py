# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence shared by the read-only coverage effect and pure classification node."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCommandSubscription(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str = Field(min_length=1)
    topic: str = Field(pattern=r"^onex\.cmd\.")


class ModelRuntimeMember(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    runtime_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    node: str = Field(min_length=1)
    topics: tuple[str, ...]
    group_id: str = Field(min_length=1)


class ModelRuntimeRegistryEndpoint(BaseModel):
    """A published manifest endpoint and its deployment-owned broker identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    host: str = Field(min_length=1)
    manifest_url: str = Field(pattern=r"^https?://")


class ModelRuntimeCoverageRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str = Field(min_length=1)
    contract_roots: tuple[str, ...] = Field(min_length=1)
    # A bus can carry several declared runtime lanes. Only these contracts are
    # expected; unrestricted contracts remain in scope too.
    runtime_lanes: tuple[str, ...] = Field(min_length=1)
    runtime_registries: tuple[ModelRuntimeRegistryEndpoint, ...] = ()


class ModelRuntimeCoverageSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str
    broker: str
    observed_at: datetime
    commands: tuple[ModelCommandSubscription, ...]
    members: tuple[ModelRuntimeMember, ...] = ()
    error: str = ""
    # Preserve per-node failures without discarding readable nodes.
    unreadable_nodes: tuple[str, ...] = ()
    registry_errors: tuple[str, ...] = ()


class ModelCommandCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str
    topic: str
    status: Literal["OK", "FAIL"]
    serving_runtimes: int | None = Field(default=None, ge=0)
    runtime_ids: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    detail: str


class ModelRuntimeCoverageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bus_lane: str
    broker: str
    observed_at: datetime
    status: Literal["OK", "FAIL"]
    commands: tuple[ModelCommandCoverage, ...] = ()
    error: str = ""
    registry_errors: tuple[str, ...] = ()
