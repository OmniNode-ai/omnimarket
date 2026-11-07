# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and vocabulary of the host storage effect (OMN-22906).

A request names a host and one DECLARED storage command. The result carries the free bytes read on
the command's measured path before and after, so a window's storage step has a typed reading.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


@unique
class EnumHostStorageBackend(StrEnum):
    """Which backend runs the commands: chosen by the overlay."""

    IN_MEMORY = "in_memory"
    SUBPROCESS = "subprocess"


@unique
class EnumHostStorageOutcome(StrEnum):
    OK = "ok"
    # The node refused before running anything. See ``refusal``.
    REFUSED = "refused"
    # The command ran and exited non-zero or timed out.
    FAILED = "failed"


@unique
class EnumHostStorageRefusal(StrEnum):
    HOST_UNKNOWN = "host_unknown"
    COMMAND_UNKNOWN = "command_unknown"
    COMMAND_NOT_DECLARED_FOR_HOST = "command_not_declared_for_host"
    # Free bytes before the command could not be read; a storage command does not run blind.
    FREE_BYTES_UNREADABLE = "free_bytes_unreadable"


class ModelHostStorageRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    command: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    requested_by: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]


class ModelHostStorageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    command: str
    outcome: EnumHostStorageOutcome
    refusal: EnumHostStorageRefusal | None = None
    detail: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    free_bytes_before: int | None = None
    free_bytes_after: int | None = None

    @property
    def freed_bytes(self) -> int | None:
        if self.free_bytes_before is None or self.free_bytes_after is None:
            return None
        return self.free_bytes_after - self.free_bytes_before


class ModelHostStorageRun(BaseModel):
    """What a backend reports for one command run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    timed_out: bool = False
    # A short tail of the command's output, for the result's detail.
    output_tail: str = ""
