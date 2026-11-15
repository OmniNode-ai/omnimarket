# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one lab host announces about itself on the bus (OMN-24367).

The announcement is the host's identity, roles, graphics cards, models served, runtimes, installed software,
lane accounts and availability (``ModelHostFacts``, the part that changes only when the host changes), beside
its capacity (``ModelHostCapacityAdvertisement``, the part that moves with load, carried unchanged from the
capacity advertisement published at the same beat). ``facts_digest`` hashes the facts and not the capacity, so
a reader tells "the host changed" from "the host is busy".
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)


class EnumAnnounceReason(StrEnum):
    """Why this announcement was published."""

    START = "start"
    FACTS_CHANGED = "facts_changed"
    HEARTBEAT = "heartbeat"
    STOP = "stop"


class ModelHostGpu(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    memory_total_mib: int | None = Field(default=None, ge=0)


class ModelHostServedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    port: int | None = Field(default=None, ge=1, le=65535)


class ModelHostRuntime(BaseModel):
    """A container or language runtime on the host: docker, python, node and the like."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str


class ModelHostSoftware(BaseModel):
    """One installed program with the version and, where the host knows it, the commit it was built from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str = ""
    commit: str = ""


class ModelHostLaneAccount(BaseModel):
    """One account lanes can run work as on this host, and whether it is signed in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    signed_in: bool = False


class ModelHostAvailability(BaseModel):
    """Whether the host takes work at all, and the reason when it does not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    takes_work: bool = True
    reason: str = ""


class ModelHostFacts(BaseModel):
    """What a host is, as read from the host: every field here goes into the digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    roles: list[str] = Field(default_factory=list)
    cores: int = Field(..., ge=1)
    mem_total_bytes: int | None = Field(default=None, ge=1)
    gpus: list[ModelHostGpu] = Field(default_factory=list)
    models_served: list[ModelHostServedModel] = Field(default_factory=list)
    runtimes: list[ModelHostRuntime] = Field(default_factory=list)
    software: list[ModelHostSoftware] = Field(default_factory=list)
    lane_accounts: list[ModelHostLaneAccount] = Field(default_factory=list)
    availability: ModelHostAvailability = Field(default_factory=ModelHostAvailability)


class ModelHostIntrospectionRequest(BaseModel):
    """The facts and the capacity read at one beat, and why the host is announcing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelHostFacts
    capacity: ModelHostCapacityAdvertisement
    reason: EnumAnnounceReason
    announced_at: datetime


class ModelHostAnnouncement(BaseModel):
    """One host's announcement: its facts, its capacity and the digest of the facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    reason: EnumAnnounceReason
    announced_at: datetime
    facts_digest: str
    facts: ModelHostFacts
    capacity: ModelHostCapacityAdvertisement
