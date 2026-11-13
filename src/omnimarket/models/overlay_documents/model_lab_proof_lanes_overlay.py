# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/lab_proof_lanes/overlay.yaml through (OMN-24191).

omnibase_infra reads the same document through its own resolver (.github/actions/resolve-lab-lane/
resolve_lab_lane.py); no value lives here.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_COMPOSE_PROJECT_RE = re.compile(r"^omnibase-infra(-[a-z0-9]+)*$")
_FORBIDDEN_HOSTS = frozenset(
    {
        "localhost",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)


def _validate_lane_url(field_name: str, value: str) -> str:
    """Refuse a malformed URL; store it as scheme://host:port with no trailing slash."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"{field_name} {value!r} must use http or https")
    if not parsed.hostname:
        raise ValueError(f"{field_name} {value!r} must have a hostname")
    if parsed.port is None:
        raise ValueError(f"{field_name} {value!r} must have an explicit port")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(
            f"{field_name} {value!r} must be a base URL with no path, query or fragment"
        )
    host = parsed.hostname.lower()
    if host in _FORBIDDEN_HOSTS:
        raise ValueError(f"{field_name} {value!r} names a host-local alias")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and ip.is_loopback:
        raise ValueError(f"{field_name} {value!r} names a loopback address")
    return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


class ModelLabProofLane(BaseModel):
    """A single lab proof lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    lane_role: Literal["stability"]
    compose_project: str
    bus_lane: str
    docker_runs_on: list[str] = Field(min_length=1)
    ingress_url: str
    projection_url: str
    deploy_agent_url: str
    gateway_url: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError(f"name {value!r} must match ^[a-z0-9][a-z0-9-]*$")
        return value

    @field_validator("compose_project")
    @classmethod
    def _validate_compose_project(cls, value: str) -> str:
        if not _COMPOSE_PROJECT_RE.fullmatch(value):
            raise ValueError(
                f"compose_project {value!r} must match ^omnibase-infra(-[a-z0-9]+)*$"
            )
        return value

    @field_validator("bus_lane")
    @classmethod
    def _validate_bus_lane(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError(f"bus_lane {value!r} must match ^[a-z0-9][a-z0-9-]*$")
        return value

    @field_validator("docker_runs_on")
    @classmethod
    def _validate_docker_runs_on(cls, value: list[str]) -> list[str]:
        for item in value:
            if not item:
                raise ValueError("docker_runs_on must be a list of non-empty strings")
        return value

    @field_validator("ingress_url", "projection_url", "deploy_agent_url", "gateway_url")
    @classmethod
    def _validate_urls(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _validate_lane_url(str(info.field_name), value)


class ModelLabProofLanesOverlay(BaseModel):
    """The lab proof lanes overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    variable: Literal["LAB_PROOF_LANES_JSON"]
    repository: Literal["OmniNode-ai/omnibase_infra"]
    lanes: list[ModelLabProofLane] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_lane_names(self) -> ModelLabProofLanesOverlay:
        seen: set[str] = set()
        for lane in self.lanes:
            if lane.name in seen:
                raise ValueError(f"lane {lane.name!r} is declared twice")
            seen.add(lane.name)
        return self

    def render_variable_value(self) -> str:
        """Render the exact JSON value of the LAB_PROOF_LANES_JSON variable."""
        lane_dicts = [lane.model_dump(exclude_none=True) for lane in self.lanes]
        return json.dumps(lane_dicts, separators=(",", ":"), ensure_ascii=True)


__all__ = [
    "ModelLabProofLane",
    "ModelLabProofLanesOverlay",
]
