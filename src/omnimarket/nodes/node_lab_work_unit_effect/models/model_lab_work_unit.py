# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab work unit and the pool host capacity advertisement (OMN-20105).

A LAB WORK UNIT is one heavy command (a test suite, a build, lint over a tree,
a code draft by a lab-model agent) run at one exact pushed commit of one repository, on ONE named
pool host. The caller places it (see ``omnimarket.lab_work.placement``) and
names the host on the request; each pool host's serve process runs only the
units addressed to it. The receipt carries the exit code, the log path on the
host and a bounded tail of the output.

A HOST CAPACITY ADVERTISEMENT is what each serve process publishes on a fixed
cadence: cores, load1, available memory, the tools it has and the units it is
running. ``advertised_at`` is stamped by the advertising effect, never by a
caller, and ``cadence_seconds`` travels with it so the reader's staleness bound
(``2 * cadence_seconds``) is data, not a constant (the OMN-14977 rule carried
by OMN-16738).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: A unit may run at most this long; the caller's wait adds slack on top.
MAX_TIMEOUT_SECONDS: int = 7200
#: The receipt carries at most this much of the tail of the output.
MAX_TAIL_CHARS: int = 8000

_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _check_name(value: str) -> str:
    if not _NAME.match(value):
        raise ValueError(f"not a plain name: {value!r}")
    return value


WorkKind = Literal["test", "build", "lint", "code_draft", "other"]


class EnumLabWorkUnitStatus(StrEnum):
    """How a unit ended. ``completed`` means the command ran to an exit code,
    whatever that code was; the others mean it did not."""

    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    REFUSED = "refused"
    INFRA_ERROR = "infra_error"


class ModelLabWorkUnitRequest(BaseModel):
    """One heavy command at one pushed commit, addressed to one pool host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_unit_id: str = Field(..., min_length=8, max_length=64)
    target_host: str
    repo: str = Field(..., description="owner/name on GitHub")
    commit_sha: str
    argv: list[str] = Field(..., min_length=1, max_length=256)
    kind: WorkKind = "other"
    timeout_seconds: int = Field(default=1800, ge=1, le=MAX_TIMEOUT_SECONDS)
    lane: str
    need_tools: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("target_host", "lane", "work_unit_id")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _check_name(value)

    @field_validator("need_tools")
    @classmethod
    def _tools(cls, value: list[str]) -> list[str]:
        return [_check_name(tool) for tool in value]

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        if not _REPO.match(value):
            raise ValueError(f"repo must be owner/name: {value!r}")
        return value

    @field_validator("commit_sha")
    @classmethod
    def _sha(cls, value: str) -> str:
        if not _SHA.match(value):
            raise ValueError("commit_sha must be a full 40-hex sha")
        return value

    @field_validator("argv")
    @classmethod
    def _argv(cls, value: list[str]) -> list[str]:
        if any((not word) or len(word) > 20_000 or "\x00" in word for word in value):
            raise ValueError("argv words must be non-empty, NUL-free and bounded")
        return value


class ModelLabWorkUnitReceipt(BaseModel):
    """What the host that ran (or refused) a unit answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    work_unit_id: str
    target_host: str
    host: str
    repo: str
    commit_sha: str
    status: EnumLabWorkUnitStatus
    exit_code: int | None = None
    log_path: str = ""
    output_tail: str = Field(default="", max_length=MAX_TAIL_CHARS)
    duration_seconds: float = 0.0
    detail: str = Field(default="", max_length=2000)


class ModelHostCapacityProbeRequest(BaseModel):
    """What the serve process asks its advertising effect for on each beat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    cadence_seconds: int = Field(default=10, ge=1, le=600)
    tools: list[str] = Field(default_factory=list, max_length=32)
    running_units: int = Field(default=0, ge=0)
    max_units: int = Field(default=1, ge=1, le=64)
    rank_penalty: float = Field(default=0.0, ge=0.0, le=10.0)

    @field_validator("host_name")
    @classmethod
    def _plain(cls, value: str) -> str:
        return _check_name(value)


class ModelHostCapacityAdvertisement(BaseModel):
    """One pool host's capacity at ``advertised_at``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    cores: int = Field(..., ge=1)
    load1: float = Field(..., ge=0.0)
    mem_available_bytes: int = Field(..., ge=0)
    tools: list[str] = Field(default_factory=list)
    running_units: int = Field(default=0, ge=0)
    max_units: int = Field(default=1, ge=1)
    #: Added to load per core for RANKING only, never for the load bar: it keeps an
    #: evidence-lane host last-resort while idle hosts take the work (OMN-17485).
    rank_penalty: float = Field(default=0.0, ge=0.0)
    advertised_at: datetime
    cadence_seconds: int = Field(..., ge=1)

    @property
    def load_per_core(self) -> float:
        """Load per core, counting the units this host already runs."""
        return (self.load1 + self.running_units) / self.cores


__all__ = [
    "MAX_TAIL_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "EnumLabWorkUnitStatus",
    "ModelHostCapacityAdvertisement",
    "ModelHostCapacityProbeRequest",
    "ModelLabWorkUnitReceipt",
    "ModelLabWorkUnitRequest",
    "WorkKind",
]
