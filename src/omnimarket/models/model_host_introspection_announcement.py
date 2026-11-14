# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One host's introspection announcement, and the failure that stands in for it (OMN-19998).

A lab host, a GPU model host or, later, a cloud cluster says what it is on the bus at start, when its facts
change and on a heartbeat; the registry subscribes and placement, hot loading and the board read the registry
(RULING 2026-11-14T03:36:15Z). The announcement composes the existing capacity advertisement
(``ModelHostCapacityAdvertisement``) as its ``capacity`` field, so the advertisement placement reads today is a
strict subset of it and the two can be compared field for field until the registry reads this instead.

Neither the producer node nor the registry imports the other's models, so the shape lives here, like the
advertisement. ``facts_digest`` hashes the facts that do not move on every beat, so a reader sees a change
without diffing; :meth:`ModelHostIntrospectionAnnouncement.expected_facts_digest` is the one rule for it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)

ANNOUNCEMENT_SCHEMA_VERSION: Literal[1] = 1


class EnumHostAnnouncementReason(StrEnum):
    """Why this announcement was published."""

    START = "start"
    HEARTBEAT = "heartbeat"
    CHANGE = "change"
    STOP = "stop"


class EnumSoftwareKind(StrEnum):
    """What an installed-software row names."""

    PLUGIN = "plugin"
    PACKAGE = "package"
    CLONE = "clone"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHostIdentity(_Frozen):
    """Who the machine says it is."""

    hostnames: tuple[str, ...] = Field(..., min_length=1)
    os: str
    arch: str
    kernel: str
    #: Changes when the machine reboots; None where the platform gives none.
    boot_id: str | None = None


class ModelHostGpu(_Frozen):
    """One GPU. ``mem_used_bytes`` moves with load and is left out of the facts digest."""

    name: str
    mem_total_bytes: int = Field(..., ge=0)
    mem_used_bytes: int = Field(..., ge=0)


class ModelHostModelServed(_Frozen):
    """One model the host serves, and where."""

    name: str
    port: int = Field(..., ge=1, le=65535)
    endpoint: str


class ModelHostRuntime(_Frozen):
    """One runtime the host has installed (python, docker, claude, codex, uv ...); None version: present, unreadable."""

    name: str
    version: str | None = None


class ModelHostSoftware(_Frozen):
    """One installed plugin, package or clone with its version and commit."""

    name: str
    kind: EnumSoftwareKind
    version: str | None = None
    commit: str | None = None


class ModelHostLaneAccount(_Frozen):
    """An account name and whether it can run lanes here now; never a token."""

    account_name: str
    can_run_lanes: bool


class ModelHostAvailability(_Frozen):
    """Whether the host takes work, and the sentence that says why not."""

    available: bool
    reason: str = ""


class ModelHostFacts(_Frozen):
    """Everything about the host that is read from the machine, apart from its capacity advertisement."""

    identity: ModelHostIdentity
    gpus: tuple[ModelHostGpu, ...] = ()
    models_served: tuple[ModelHostModelServed, ...] = ()
    runtimes: tuple[ModelHostRuntime, ...] = ()
    software: tuple[ModelHostSoftware, ...] = ()


class ModelHostIntrospectionAnnouncement(_Frozen):
    """One host's announcement at ``announced_at``."""

    schema_version: Literal[1] = ANNOUNCEMENT_SCHEMA_VERSION
    #: ``host`` today; a cloud cluster or tenant is another kind on the same shape.
    announcement_kind: Literal["host"] = "host"
    reason: EnumHostAnnouncementReason
    host_name: str = Field(..., min_length=1)
    announced_at: datetime
    announcement_id: UUID
    cadence_seconds: int = Field(..., ge=1)
    identity: ModelHostIdentity
    #: The placement roles of the host's row in the lab host table; empty when the host has no row there.
    roles: tuple[str, ...] = ()
    capacity: ModelHostCapacityAdvertisement
    gpus: tuple[ModelHostGpu, ...] = ()
    models_served: tuple[ModelHostModelServed, ...] = ()
    runtimes: tuple[ModelHostRuntime, ...] = ()
    software: tuple[ModelHostSoftware, ...] = ()
    lane_accounts: tuple[ModelHostLaneAccount, ...] = ()
    availability: ModelHostAvailability
    facts_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    def expected_facts_digest(self) -> str:
        """The sha256 of the facts that do not move on every beat, in a fixed key order."""
        return facts_digest(
            identity=self.identity,
            roles=self.roles,
            cores=self.capacity.cores,
            mem_total_bytes=self.capacity.mem_total_bytes,
            gpus=self.gpus,
            models_served=self.models_served,
            runtimes=self.runtimes,
            software=self.software,
            lane_accounts=self.lane_accounts,
            availability=self.availability,
        )

    @model_validator(mode="after")
    def _digest_matches_facts(self) -> ModelHostIntrospectionAnnouncement:
        expected = self.expected_facts_digest()
        if self.facts_digest != expected:
            raise ValueError(
                f"facts_digest {self.facts_digest} does not match the announcement's facts ({expected})"
            )
        return self


class ModelHostIntrospectionFailure(_Frozen):
    """What the announcer publishes when it could not read the host: the registry sees a live host it cannot read.

    A host whose facts are unreadable publishes this and no announcement, so a reader never takes a zero-filled
    reading for a host and never mistakes an unreadable host for a silent one.
    """

    host_name: str = Field(..., min_length=1)
    failed_at: datetime
    reason: EnumHostAnnouncementReason
    error_type: str
    error: str


def facts_digest(
    *,
    identity: ModelHostIdentity,
    roles: tuple[str, ...],
    cores: int,
    mem_total_bytes: int | None,
    gpus: tuple[ModelHostGpu, ...],
    models_served: tuple[ModelHostModelServed, ...],
    runtimes: tuple[ModelHostRuntime, ...],
    software: tuple[ModelHostSoftware, ...],
    lane_accounts: tuple[ModelHostLaneAccount, ...],
    availability: ModelHostAvailability,
) -> str:
    """The digest rule: the stable facts only (no load, free memory, GPU memory in use or time), keys sorted."""
    stable = {
        "identity": identity.model_dump(mode="json"),
        "roles": list(roles),
        "cores": cores,
        "mem_total_bytes": mem_total_bytes,
        "gpus": [
            {"name": gpu.name, "mem_total_bytes": gpu.mem_total_bytes} for gpu in gpus
        ],
        "models_served": [m.model_dump(mode="json") for m in models_served],
        "runtimes": [r.model_dump(mode="json") for r in runtimes],
        "software": [s.model_dump(mode="json") for s in software],
        "lane_accounts": [a.model_dump(mode="json") for a in lane_accounts],
        "availability": availability.model_dump(mode="json"),
    }
    body = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


__all__ = [
    "ANNOUNCEMENT_SCHEMA_VERSION",
    "EnumHostAnnouncementReason",
    "EnumSoftwareKind",
    "ModelHostAvailability",
    "ModelHostFacts",
    "ModelHostGpu",
    "ModelHostIdentity",
    "ModelHostIntrospectionAnnouncement",
    "ModelHostIntrospectionFailure",
    "ModelHostLaneAccount",
    "ModelHostModelServed",
    "ModelHostRuntime",
    "ModelHostSoftware",
    "facts_digest",
]
