# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inputs to the floor publisher's projection-backed delegation read."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelDelegationReportGroup(BaseModel):
    """A display group for observed attempts, declared by the report contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    tiers: tuple[str, ...] = ()
    backend_prefixes: tuple[str, ...] = ()
    backend_contains: tuple[str, ...] = ()
    model_prefixes: tuple[str, ...] = ()


class ModelDelegationReportPolicy(BaseModel):
    """Display classification of recorded traffic; it never selects a backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: tuple[ModelDelegationReportGroup, ...] = Field(min_length=1)
    local_group: str = Field(min_length=1)
    fallback_group: str = Field(min_length=1)


class ModelLocalProbeEndpoint(BaseModel):
    """Optional physical endpoint inventory, separate from configured routing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    endpoint_url: str = Field(pattern=r"^https?://[^@?\s]+/v1/chat/completions$")
    source: str = Field(min_length=1)


class ModelDelegationReadOverlay(BaseModel):
    """The existing delegation-health overlay supplies the deployed read surface."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    compose_project: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    postgres_service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    runtime_service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    database: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    db_user: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    contract_path: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")
    probe_endpoints: tuple[ModelLocalProbeEndpoint, ...] = ()


class ModelDelegationReportRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probe: bool = False
    lanes_running: int = Field(default=0, ge=0)
    overlay: ModelDelegationReadOverlay | None = None
    # The seconds the lab projection read may take; the publish node passes its contract's cycle number (OMN-22699).
    timeout_seconds: int = Field(default=120, gt=0)


class ModelDelegationReportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report: dict[str, Any]
    tick: str
