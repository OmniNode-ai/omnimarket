# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed install request, result, decision and owed reading (OMN-19998, root 5: the install request).

A lane that ends with a change that needs an install on a named host (a lab host or the launching Mac) publishes one
``ModelInstallRequest`` on the bus. The installer node on that host decides with the pure decision compute, runs the
declared installer when it is safe, and publishes one ``ModelInstallRequestResult`` carrying the line the verify command
printed. Until a ``verified`` result exists the request owns the ``NOT RUNNING`` line; one not verified inside its
expiry is the ``expired`` line the throughput tick shows. Every model here is shared by the decision compute, the owed
reading compute and the effect node, so a change of shape in one breaks the seam test of the others.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A repository is ``owner/name``; a ref is a branch name or a commit sha and can never read as an option.
REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
REF_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$"
UNIT_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$"

#: The two words the owed line carries; the throughput tick matches on them (the seam test pins both).
NOT_RUNNING_MARK = "NOT RUNNING"
EXPIRED_MARK = "INSTALL-REQUEST-EXPIRED"


class EnumInstallTarget(StrEnum):
    """What the request installs: a code tree a unit runs from, or the launching host's plugin tree."""

    UNIT_TREE = "unit-tree"
    PLUGIN_TREE = "plugin-tree"


class EnumInstallRequestAction(StrEnum):
    """What the installer on a host does with one request."""

    EXECUTE = "execute"
    HOLD = "hold"
    EXPIRE = "expire"
    SKIP_DONE = "skip_done"
    IGNORE = "ignore"


class EnumInstallRequestStatus(StrEnum):
    """How a request ended; only ``verified`` clears the request's ``NOT RUNNING`` line."""

    VERIFIED = "verified"
    VERIFY_FAILED = "verify_failed"
    INSTALL_FAILED = "install_failed"
    REFUSED = "refused"
    EXPIRED = "expired"


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must carry a time zone")


class ModelInstallRequestDraft(BaseModel):
    """What a lane gives the publisher; the publisher adds the id and both times from the overlay's expiry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    host: str = Field(min_length=1)
    kind: EnumInstallTarget
    repository: str = Field(pattern=REPOSITORY_PATTERN)
    ref: str = Field(pattern=REF_PATTERN)
    unit: str = Field(pattern=UNIT_PATTERN)
    verify_command: tuple[str, ...] = Field(min_length=1)
    expected_line: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelInstallRequestDraft:
        if ".." in self.ref.split("/"):
            raise ValueError("ref must not contain '..'")
        if any(not part for part in self.verify_command):
            raise ValueError("verify_command holds an empty argument")
        return self


class ModelInstallRequest(ModelInstallRequestDraft):
    """One request to install one unit or tree on one host, with the check that proves it runs."""

    request_id: UUID
    requested_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _times(self) -> ModelInstallRequest:
        _aware(self.requested_at, "requested_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be after requested_at")
        return self


class ModelInstallRequestResult(BaseModel):
    """The typed result the installer on the host publishes: the revision it installed and the verify line it read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    host: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    status: EnumInstallRequestStatus
    at: datetime
    #: The revision the installer reports (a commit sha); empty when none was installed.
    revision: str = ""
    #: The first line of the verify command's output that holds the expected line; empty when none did.
    verify_line: str = ""
    #: A short code: ``ok``, ``unit_unknown``, ``installer_failed``, ``verify_mismatch``, ``expired`` and so on.
    reason: str = ""
    detail: str = ""

    @model_validator(mode="after")
    def _time(self) -> ModelInstallRequestResult:
        _aware(self.at, "at")
        if self.status is EnumInstallRequestStatus.VERIFIED and not self.verify_line:
            raise ValueError("a verified result carries the verify line")
        return self

    def integration_line(self) -> str:
        """The ``installed=<host>:<revision> observed=<verify line>`` words a handoff reads; empty unless verified with a revision."""
        if self.status is not EnumInstallRequestStatus.VERIFIED or not self.revision:
            return ""
        return f"installed={self.host}:{self.revision} observed={self.verify_line}"


class ModelInstallRequestDecisionInput(BaseModel):
    """The facts the installer on ``executor_host`` read: the request, the clock, any result already published."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelInstallRequest
    executor_host: str = Field(min_length=1)
    now: datetime
    prior_result: ModelInstallRequestResult | None = None
    #: Reasons the host is not safe to install on right now (an install lock held, a unit mid-run); empty means safe.
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _time(self) -> ModelInstallRequestDecisionInput:
        _aware(self.now, "now")
        return self


class ModelInstallRequestDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    action: EnumInstallRequestAction
    reason: str
    detail: str = ""


class ModelInstallOwedInput(BaseModel):
    """Every request and every result a reader holds, and the clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: tuple[ModelInstallRequest, ...] = ()
    results: tuple[ModelInstallRequestResult, ...] = ()
    now: datetime

    @model_validator(mode="after")
    def _time(self) -> ModelInstallOwedInput:
        _aware(self.now, "now")
        return self


class ModelInstallOwedLine(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    kind: Literal["not_running", "expired"]
    line: str


class ModelInstallOwedReading(BaseModel):
    """``owed`` counts the requests with no verified result; ``expired`` counts those of them past their expiry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owed: int = Field(ge=0)
    expired: int = Field(ge=0)
    lines: tuple[ModelInstallOwedLine, ...] = ()


__all__: list[str] = [
    "EXPIRED_MARK",
    "NOT_RUNNING_MARK",
    "REF_PATTERN",
    "REPOSITORY_PATTERN",
    "UNIT_PATTERN",
    "EnumInstallRequestAction",
    "EnumInstallRequestStatus",
    "EnumInstallTarget",
    "ModelInstallOwedInput",
    "ModelInstallOwedLine",
    "ModelInstallOwedReading",
    "ModelInstallRequest",
    "ModelInstallRequestDecision",
    "ModelInstallRequestDecisionInput",
    "ModelInstallRequestDraft",
    "ModelInstallRequestResult",
]
