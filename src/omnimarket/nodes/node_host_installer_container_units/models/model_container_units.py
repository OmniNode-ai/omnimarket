# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay, request and result of the host container unit installer (OMN-23390)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Mode = Literal["install", "status", "uninstall", "dry_run"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelContainerPort(_Frozen):
    host_address: NonEmptyStr
    host_port: int = Field(gt=0, lt=65536)
    container_port: int = Field(gt=0, lt=65536)


class ModelContainerCreate(_Frozen):
    image: NonEmptyStr
    command: tuple[NonEmptyStr, ...] = ()
    volumes: tuple[NonEmptyStr, ...] = ()
    restart_policy: NonEmptyStr
    env_file: NonEmptyStr | None = None


class ModelContainerUnit(_Frozen):
    """One declared unit: ``name`` is the unit file stem, ``container`` the docker container it keeps running."""

    name: NonEmptyStr
    hosts: tuple[NonEmptyStr, ...] = Field(min_length=1)
    container: NonEmptyStr
    ticket: NonEmptyStr
    docker_binary: NonEmptyStr
    restart_seconds: int = Field(gt=0)
    stop_timeout_seconds: int = Field(gt=0)
    probe_address: NonEmptyStr
    probe_port: int = Field(gt=0, lt=65536)
    probe_wait_seconds: int = Field(gt=0)
    network: NonEmptyStr | None = None
    published_ports: tuple[ModelContainerPort, ...] = ()
    create: ModelContainerCreate | None = None

    @model_validator(mode="after")
    def _network_required_for_ports_or_create(self) -> ModelContainerUnit:
        if (self.published_ports or self.create) and not self.network:
            raise ValueError(
                "network is required when published_ports or create is declared"
            )
        return self


class ModelContainerUnitsOverlay(_Frozen):
    units: tuple[ModelContainerUnit, ...] = Field(min_length=1)


class ModelContainerUnitsRequest(_Frozen):
    host_name: NonEmptyStr
    unit: NonEmptyStr
    mode: Mode
    overlay_file: str = ""


class ModelContainerUnitsResult(_Frozen):
    exit_code: int
    stdout: str = ""
    stderr: str = ""
