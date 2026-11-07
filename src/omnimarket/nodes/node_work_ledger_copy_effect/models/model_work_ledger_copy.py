# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed settings, facts and results of the interim work-ledger second copy (OMN-22478)."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EnumCopyOutcome(StrEnum):
    COPIED_VERIFIED = "copied-verified"
    REFUSED_LOW_SPACE = "refused-low-space"
    DUMP_FAILED = "dump-failed"
    TRANSFER_MISMATCH = "transfer-mismatch"
    DRILL_FAILED = "drill-failed"
    DRILL_MISMATCH = "drill-mismatch"


class ModelCopySource(BaseModel):
    """The projection database the copy reads. The password is read from ``password_env``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    #: A dotenv file the password variable is read from when it is not in the process environment.
    env_file: str | None = None
    pg_bin_dir: str = Field(min_length=1)
    schema_name: str = Field(min_length=1)
    rows_table: str = Field(min_length=1)
    state_table: str = Field(min_length=1)
    ledger_id: str = Field(min_length=1)


class ModelCopyFile(BaseModel):
    """The markdown ledger and its rolls, copied beside the projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path_env: str = Field(min_length=1)
    archive_dirname: str = Field(min_length=1)


class ModelCopyDrill(BaseModel):
    """The restore drill: a throwaway database in a container on the target host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = Field(min_length=1)
    memory_limit: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    scratch_database: str = Field(min_length=1)


class ModelCopyTarget(BaseModel):
    """Where the second copy lives: an ssh destination that is not the database host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_target: str = Field(min_length=1)
    directory: str = Field(min_length=1)
    keep: int = Field(ge=1)
    min_free_bytes: int = Field(ge=1)
    drill: ModelCopyDrill

    @field_validator("directory")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not PurePosixPath(value).is_absolute():
            raise ValueError("target directory must be an absolute path")
        return value


class ModelCopyReport(BaseModel):
    """How one run is reported: a command whose last argument is the row text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    append_argv: tuple[str, ...] = Field(min_length=1)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)


class ModelCopyLaunchd(BaseModel):
    """The launchd agent that runs the copy: interpreter, environment and where its logs go."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interpreter: str = Field(min_length=1)
    working_directory: str = Field(min_length=1)
    log_directory: str = Field(min_length=1)
    environment: dict[str, str]
    max_runtime_seconds: int = Field(ge=1)


class ModelCopyOverlay(BaseModel):
    """Every deployment fact of the node. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: ModelCopySource
    file: ModelCopyFile
    target: ModelCopyTarget
    report: ModelCopyReport
    launchd: ModelCopyLaunchd
    timeout_seconds: int = Field(ge=1)


class ModelRowFacts(BaseModel):
    """What a database holds of the ledger rows no later than a cutoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_count: int = Field(ge=0)
    last_row_id: str
    state_count: int = Field(ge=0)


class ModelFileFacts(BaseModel):
    """What a ledger file copy holds: its size, line count and last line's digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    size_bytes: int = Field(ge=0)
    line_count: int = Field(ge=0)
    last_line_sha256: str


class ModelCopyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9T_-]+$")
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("started_at must be timezone-aware UTC")
        return value


class ModelCopyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    outcome: EnumCopyOutcome
    detail: str
    source_rows: ModelRowFacts | None = None
    restored_rows: ModelRowFacts | None = None
    source_file: ModelFileFacts | None = None
    restored_file: ModelFileFacts | None = None
    copy_bytes: int = Field(default=0, ge=0)
    target_free_bytes: int | None = None
    pruned: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome is EnumCopyOutcome.COPIED_VERIFIED
