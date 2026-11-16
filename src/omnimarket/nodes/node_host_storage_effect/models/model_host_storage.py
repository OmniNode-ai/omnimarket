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
    # The command exited zero without timing out, but the free bytes AFTER it could not be read: the
    # write happened and the before/after reading the contract promises is incomplete.
    COMPLETED_UNOBSERVED = "completed_unobserved"
    # An entry command removed some of the named entries and refused or failed on others.
    PARTIAL = "partial"


@unique
class EnumHostStorageRefusal(StrEnum):
    HOST_UNKNOWN = "host_unknown"
    COMMAND_UNKNOWN = "command_unknown"
    COMMAND_NOT_DECLARED_FOR_HOST = "command_not_declared_for_host"
    # Free bytes before the command could not be read; a storage command does not run blind.
    FREE_BYTES_UNREADABLE = "free_bytes_unreadable"
    # An entry command was sent with no entries, or an argv command was sent with entries.
    ENTRIES_REQUIRED = "entries_required"
    ENTRIES_NOT_ACCEPTED = "entries_not_accepted"
    # Every named entry was refused (outside the declared roots, in use or absent); nothing was removed.
    ALL_ENTRIES_REFUSED = "all_entries_refused"


@unique
class EnumHostStorageEntryOutcome(StrEnum):
    """What happened to one named entry of an entry command."""

    REMOVED = "removed"
    # The entry is a relative path, resolves outside every root the host declares for the command (a
    # symlink or ``..`` included), is a symlink itself or is the root itself.
    REFUSED_OUTSIDE_ROOT = "refused_outside_root"
    # A live process holds the entry, a lease marks it, or the process table could not be read.
    REFUSED_IN_USE = "refused_in_use"
    REFUSED_NOT_FOUND = "refused_not_found"
    # The removal started and did not finish, or the command's timeout passed before this entry.
    FAILED = "failed"


class ModelHostStorageRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    command: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    requested_by: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
    # The entries an entry command is sent to remove (absolute paths); empty for an argv command.
    entries: tuple[Annotated[str, Field(min_length=1, max_length=4096)], ...] = Field(
        default=(), max_length=512
    )
    # The workflow run this command belongs to; echoed on the result so the stages join on it.
    correlation_id: (
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")] | None
    ) = None


class ModelHostStorageEntryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: str
    outcome: EnumHostStorageEntryOutcome
    freed_bytes: int | None = None
    detail: str | None = None


class ModelHostStorageResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    command: str
    correlation_id: str | None = None
    outcome: EnumHostStorageOutcome
    refusal: EnumHostStorageRefusal | None = None
    detail: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    free_bytes_before: int | None = None
    free_bytes_after: int | None = None
    # One row per named entry, in the order sent; empty for an argv command.
    entry_results: tuple[ModelHostStorageEntryResult, ...] = ()

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


class ModelHostStorageEntryRun(BaseModel):
    """What a backend reports for one entry command run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_results: tuple[ModelHostStorageEntryResult, ...]
    timed_out: bool = False
