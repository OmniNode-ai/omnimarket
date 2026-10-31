# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor publish node: its run request and result, the contract's runtime block, the overlay."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelFactoryFloorPublishOverlay(BaseModel):
    """Deployment facts of the host that serves the page. There is no packaged default for any of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_target: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
    remote_dir: str = Field(min_length=1)
    serve_url: str = Field(min_length=1)
    bus_lane: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

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


StepKind = Literal["module", "capture", "copy"]


class ModelPublishStep(BaseModel):
    """One input step run before the record is read.

    ``module`` runs ``python -m <module> <args>``; ``capture`` runs ``argv`` and keeps its standard output as a
    JSON list at ``output_relpath``; ``copy`` copies ``source_relpath`` to ``output_relpath``. ``{home}``,
    ``{bus_lane}`` in ``args`` and ``argv`` are the registry home and the overlay's bus lane. A step that is not
    ``required`` may fail: the failure is recorded in the status file and the page shows its source as unread.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    kind: StepKind
    required: bool
    timeout_seconds: int = Field(gt=0)
    module: str | None = None
    args: tuple[str, ...] = ()
    argv: tuple[str, ...] = ()
    source_relpath: str | None = None
    output_relpath: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelPublishStep:
        need = {
            "module": bool(self.module),
            "capture": bool(self.argv) and bool(self.output_relpath),
            "copy": bool(self.source_relpath) and bool(self.output_relpath),
        }[self.kind]
        if not need:
            raise ValueError(
                f"step {self.name} lacks the fields its kind {self.kind} needs"
            )
        return self


class ModelPublishStepResult(BaseModel):
    """What one step did. ``detail`` is an exit code or a reason and is published; ``diagnostic`` is the tail of the
    program's error output, for the tick's own log, and is never published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    status: Literal["ok", "failed"]
    detail: str = ""
    diagnostic: str = ""


class ModelPublishNames(BaseModel):
    """The file names the server serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page: str = Field(min_length=1)
    data: str = Field(min_length=1)
    process_health: str = Field(min_length=1)
    status: str = Field(min_length=1)


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
    process_health_relpath: str = Field(min_length=1)
    steps: tuple[ModelPublishStep, ...] = Field(min_length=1)
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
    steps: tuple[ModelPublishStepResult, ...]
    process_health_published: bool
