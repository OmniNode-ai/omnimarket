# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs of the host introspection compute, and the ``host_introspection`` overlay shape (OMN-19998).

The compute is pure: the caller reads the machine and the lab host table and hands the readings over with the
time and the announcement id, so the same request always gives the same announcement. The overlay holds every
value the effect reads with (cadence, probes, timeouts, account tools); no value is written in code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_introspection_announcement import (
    EnumHostAnnouncementReason,
    EnumSoftwareKind,
    ModelHostFacts,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHostRowFacts(_Frozen):
    """What the host's row in the lab host table says; a host with no row there passes ``declared=False``."""

    declared: bool
    roles: tuple[str, ...] = ()
    available: bool = True
    unavailable_reason: str | None = None


class ModelLaneAccountDeclaration(_Frozen):
    """An account the overlay declares for the host: it can run lanes while ``tool`` is among the advertised tools."""

    account_name: NonEmpty
    tool: NonEmpty


class ModelHostCloneTarget(_Frozen):
    """One clone the host's table row declares: the name the announcement reports and its path on the host."""

    name: NonEmpty
    path: NonEmpty


class ModelHostReadTargets(_Frozen):
    """What the host's table row tells the reader to look at; empty for a host the table does not list."""

    host_name: NonEmpty
    #: The port of the lab model the host serves, from the row's ``model_port``; None: serves none.
    model_port: int | None = Field(default=None, ge=1, le=65535)
    clones: tuple[ModelHostCloneTarget, ...] = ()


class ModelHostIntrospectionRequest(_Frozen):
    """One announcement to assemble."""

    host_name: NonEmpty
    reason: EnumHostAnnouncementReason
    announced_at: datetime
    announcement_id: UUID
    cadence_seconds: int = Field(..., ge=1)
    facts: ModelHostFacts
    capacity: ModelHostCapacityAdvertisement
    row: ModelHostRowFacts
    lane_accounts: tuple[ModelLaneAccountDeclaration, ...] = ()


class ModelProbeSpec(_Frozen):
    """A command run without a shell; ``$NAME`` in an argument is expanded from the process environment."""

    name: NonEmpty
    argv: Annotated[tuple[NonEmpty, ...], Field(min_length=1)]
    #: A regular expression with one group that picks the value out of the first output line; omitted: the whole line.
    value_regex: str | None = None


class ModelSoftwareProbe(_Frozen):
    """One installed thing to report: how to read its version and its commit (each optional)."""

    name: NonEmpty
    kind: EnumSoftwareKind
    version: ModelProbeSpec | None = None
    commit: ModelProbeSpec | None = None


class ModelHostIntrospectionOverlay(_Frozen):
    """The ``host_introspection`` overlay: every value the announcer reads with."""

    cadence_seconds: int = Field(..., ge=1)
    probe_timeout_seconds: float = Field(..., gt=0)
    #: Tools the capacity reading looks for on the PATH; the account tools below must be among them.
    capacity_tools: Annotated[tuple[NonEmpty, ...], Field(min_length=1)]
    #: The capacity reading's slot count (the serve process's worker pool is its own; this host's announcement carries
    #: the capacity inputs only, so the slot cap stays the advertisement's to compute).
    max_units: int = Field(..., ge=1)
    runtimes: tuple[ModelProbeSpec, ...] = ()
    software: tuple[ModelSoftwareProbe, ...] = ()
    #: Per clone declared on the host's table row: how to read the commit; ``{path}`` is the row's clone path.
    clone_commit_argv: tuple[NonEmpty, ...] = ()
    #: How the GPUs are listed: one csv line per GPU of name, total MiB, used MiB.
    gpu_query_argv: tuple[NonEmpty, ...] = ()
    #: The endpoint a host's served model is announced at; ``{host}`` and ``{port}`` are filled from the row.
    model_endpoint_template: NonEmpty
    #: Where the served model names are listed under the endpoint (an OpenAI-compatible ``/models``).
    model_list_path: NonEmpty
    lane_accounts: tuple[ModelLaneAccountDeclaration, ...] = ()


__all__ = [
    "ModelHostCloneTarget",
    "ModelHostIntrospectionOverlay",
    "ModelHostIntrospectionRequest",
    "ModelHostReadTargets",
    "ModelHostRowFacts",
    "ModelLaneAccountDeclaration",
    "ModelProbeSpec",
    "ModelSoftwareProbe",
]
