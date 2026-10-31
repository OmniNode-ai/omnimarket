# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish node: its run request and result, the contract's runtime block, the overlay."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelFactoryFloorPublishOverlay(BaseModel):
    """Deployment facts of the host that serves the page. There is no packaged default for any of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_target: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
    remote_dir: str = Field(min_length=1)
    serve_url: str = Field(min_length=1)

    @field_validator("remote_dir")
    @classmethod
    def _absolute_posix(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\n" in value:
            raise ValueError("remote_dir must be an absolute path without '..'")
        return value


class ModelPublishSource(BaseModel):
    """The JSON the factory floor effect wrote, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relpath: str = Field(min_length=1)


class ModelPublishRefresh(BaseModel):
    """The module that regenerates the JSON before it is published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    module: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelPublishNames(BaseModel):
    """The file names the server serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: str = Field(min_length=1)
    data: str = Field(min_length=1)


class ModelPublishTransport(BaseModel):
    """How the files reach the serving host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_timeout_seconds: int = Field(gt=0)
    transfer_timeout_seconds: int = Field(gt=0)


class ModelFactoryFloorPublishRuntime(BaseModel):
    """The ``runtime`` block of contract.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    page_relpath: str = Field(min_length=1)
    evidence_file: str = Field(min_length=1)
    source: ModelPublishSource
    refresh: ModelPublishRefresh
    names: ModelPublishNames
    transport: ModelPublishTransport


class ModelFactoryFloorPublishContract(BaseModel):
    """Only the ``runtime`` block of the contract is read by the handler."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime: ModelFactoryFloorPublishRuntime


class ModelFactoryFloorPublishRequest(BaseModel):
    """One publish run. ``None`` takes the contract's environment names and the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home: str | None = None
    state_dir: str | None = None
    skip_refresh: bool = False
    overlay: ModelFactoryFloorPublishOverlay | None = None
    now: datetime | None = None


class ModelFactoryFloorPublishResult(BaseModel):
    """What one run published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    published: tuple[str, ...]
    generated_at: str
    data_sha256: str
    serve_url: str
    refreshed: bool
