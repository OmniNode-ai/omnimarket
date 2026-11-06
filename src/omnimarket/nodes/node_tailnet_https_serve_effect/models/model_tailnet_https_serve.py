# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed settings, requests and results of the tailnet https listener (OMN-22692)."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_LOOPBACK_UPSTREAM = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}$")


class EnumServeMode(StrEnum):
    INSTALL = "install"
    UNINSTALL = "uninstall"
    STATUS = "status"


class EnumServeBackend(StrEnum):
    IN_MEMORY = "in-memory"
    TAILSCALE = "tailscale"


class EnumServeOutcome(StrEnum):
    INSTALLED = "installed"
    ALREADY_INSTALLED = "already-installed"
    UNINSTALLED = "uninstalled"
    NOT_INSTALLED = "not-installed"
    SERVING = "serving"
    NOT_SERVING = "not-serving"
    REFUSED_DEAD_UPSTREAM = "refused-dead-upstream"
    REFUSED_FOREIGN_LISTENER = "refused-foreign-listener"
    READBACK_FAILED = "readback-failed"
    BACKEND_FAILED = "backend-failed"


#: The outcomes that are the thing asked for, or the honest answer to a status read.
OK_OUTCOMES = frozenset(
    {
        EnumServeOutcome.INSTALLED,
        EnumServeOutcome.ALREADY_INSTALLED,
        EnumServeOutcome.UNINSTALLED,
        EnumServeOutcome.NOT_INSTALLED,
        EnumServeOutcome.SERVING,
        EnumServeOutcome.NOT_SERVING,
    }
)


class ModelServeListener(BaseModel):
    """The one listener: the https port and the loopback upstream it fronts. Both live here only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    https_port: int = Field(ge=1, le=65535)
    upstream_url: str
    probe_path: str = Field(pattern=r"^/[A-Za-z0-9._~/-]*$")
    probe_timeout_seconds: int = Field(ge=1)
    #: The file the boot-time unit reads the port and upstream from. The node writes it and takes it away.
    env_file: str
    env_port_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    env_upstream_name: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")

    @field_validator("upstream_url")
    @classmethod
    def _loopback(cls, value: str) -> str:
        if not _LOOPBACK_UPSTREAM.match(value):
            raise ValueError(
                "the upstream must be http://127.0.0.1:<port> (loopback only)"
            )
        return value

    @field_validator("env_file")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute():
            raise ValueError("env_file must be an absolute path")
        return value


class ModelTailscaleBackend(BaseModel):
    """The real backend: the tailscale binary and the command prefix that gives it the rights it needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    binary: str = Field(min_length=1)
    command_prefix: tuple[str, ...]
    timeout_seconds: int = Field(ge=1)


class ModelInMemoryBackend(BaseModel):
    """The in-memory default: whether the upstream answers, and what other listeners already hold ports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    upstream_alive: bool
    served: dict[int, str]


class ModelServeReport(BaseModel):
    """How one run is reported: a command whose last argument is the row text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    append_argv: tuple[str, ...] = Field(min_length=1)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    #: Variables laid over the process environment for the append command (the ledger route of the host).
    environment: dict[str, str]


class ModelServeOverlay(BaseModel):
    """Every deployment fact of the node. No key has a default.

    ``backend`` names the seam the node runs on: ``in-memory`` (needs ``in_memory``) or ``tailscale`` (needs
    ``tailscale``). The other block stays out.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumServeBackend
    listener: ModelServeListener
    tailscale: ModelTailscaleBackend | None = None
    in_memory: ModelInMemoryBackend | None = None
    report: ModelServeReport

    @model_validator(mode="after")
    def _one_backend_block(self) -> ModelServeOverlay:
        if self.backend is EnumServeBackend.TAILSCALE and (
            self.tailscale is None or self.in_memory
        ):
            raise ValueError(
                "backend tailscale needs the tailscale block and no in_memory block"
            )
        if self.backend is EnumServeBackend.IN_MEMORY and (
            self.in_memory is None or self.tailscale
        ):
            raise ValueError(
                "backend in-memory needs the in_memory block and no tailscale block"
            )
        return self


class ModelServeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9T_-]+$")
    mode: EnumServeMode


class ModelServeResult(BaseModel):
    """``served_target`` is what the listener served on the port when the run ended (empty when nothing)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    mode: EnumServeMode
    outcome: EnumServeOutcome
    detail: str
    https_port: int
    upstream_url: str
    served_target: str = ""
    changed: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome in OK_OUTCOMES
