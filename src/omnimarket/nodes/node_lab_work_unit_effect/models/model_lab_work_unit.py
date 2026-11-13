# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab work unit and the pool host capacity advertisement (OMN-20105).

A LAB WORK UNIT is one heavy command (a test suite, a build, lint over a tree,
a code draft by a lab-model agent) run at one exact pushed commit of one repository, on ONE named
pool host. The caller places it (see ``omnimarket.nodes.node_lab_placement_compute``) and
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
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from omnibase_core.enums.enum_delegation_traffic_class import (
    EnumDelegationTrafficClass,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.model_delegated_code_edit import (
    ModelCodeEditResult,
    ModelDeclaredCheck,
)
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)

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


#: Upper bound of ``ModelLabCodeDraft.max_diff_bytes``; the caller's overlay
#: value is checked against it, never defaulted from it.
MAX_DRAFT_DIFF_BYTES: int = 5_000_000


class ModelLabCodeDraft(BaseModel):
    """What a ``code_draft`` unit asks the delegated code edit loop to do.

    The loop request's fields but ``workspace_root`` (the unit's own worktree)
    and ``correlation_id`` (derived from the unit id), plus the backend the
    whole sequence is pinned to and the diff cap. Every value comes from the
    caller (E1's overlay); ``checks`` are the overlay's draft checks, never a
    ticket contract's check or check file text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(..., min_length=1, max_length=20_000)
    writable_globs: tuple[str, ...] = Field(..., min_length=1)
    context_paths: tuple[str, ...] = ()
    file_list: tuple[str, ...] = ()
    checks: tuple[ModelDeclaredCheck, ...] = Field(..., min_length=1)
    formatter: tuple[tuple[str, ...], ...] = ()
    max_turns: int = Field(..., ge=1, le=40)
    caller_lane: str | None = None
    ticket: str | None = Field(default=None, pattern=r"^OMN-[0-9]+$")
    traffic_class: EnumDelegationTrafficClass | None = None
    source_surface: str | None = Field(default=None, min_length=1)
    backend_id: str = Field(..., min_length=1, pattern=r"^\S+$")
    max_diff_bytes: int = Field(..., ge=1, le=MAX_DRAFT_DIFF_BYTES)


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
    code_draft: ModelLabCodeDraft | None = None

    @model_validator(mode="after")
    def _draft_kind(self) -> ModelLabWorkUnitRequest:
        if self.code_draft is not None:
            if self.kind != "code_draft":
                raise ValueError("a unit with a code_draft must be kind code_draft")
            if PurePosixPath(self.argv[0]).name != "onex":
                raise ValueError("a code_draft unit runs onex code-edit run")
        return self

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
    lane: str = ""
    kind: WorkKind = "other"
    status: EnumLabWorkUnitStatus
    exit_code: int | None = None
    log_path: str = ""
    output_tail: str = Field(default="", max_length=MAX_TAIL_CHARS)
    duration_seconds: float = 0.0
    detail: str = Field(default="", max_length=2000)
    #: A ``code_draft`` unit: the worktree's staged diff against the unit's
    #: commit, taken before the worktree is removed (also on a timeout, so a
    #: killed loop still returns its edits), cut to the draft's cap.
    diff: str = ""
    diff_truncated: bool = False
    #: The loop receipt's ``result`` when the loop wrote one.
    code_edit_result: ModelCodeEditResult | None = None


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


__all__ = [
    "MAX_DRAFT_DIFF_BYTES",
    "MAX_TAIL_CHARS",
    "MAX_TIMEOUT_SECONDS",
    "EnumLabWorkUnitStatus",
    "ModelHostCapacityAdvertisement",
    "ModelHostCapacityProbeRequest",
    "ModelLabCodeDraft",
    "ModelLabWorkUnitReceipt",
    "ModelLabWorkUnitRequest",
    "WorkKind",
]
