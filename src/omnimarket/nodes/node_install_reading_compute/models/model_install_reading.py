# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of piece 10's reading (OMN-22956): the install canary's probes and the verdict.

``ModelInstallProbeFact`` is what the install canary's probe (``ModelInstallProbe`` in
``node_install_canary_effect``) reports, plus the host it ran on and when. The reading imports no
sibling node: the caller maps the canary's probe onto this fact.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict


def _utc(value: AwareDatetime, field: str) -> AwareDatetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} is not UTC: {value.isoformat()}")
    return value.astimezone(UTC)


class ModelInstallProbeFact(BaseModel):
    """One run of the install canary's probe on one host.

    ``missing`` names the files the head installs that the host's copy lacks. A ``drill`` probe ran against
    a scratch copy with ``fault`` (a root data file) deleted; a ``daily`` probe ran against the install as it is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    trigger: Literal["daily", "drill"]
    probed_at: AwareDatetime
    head: str = Field(min_length=1)
    expected: int = Field(ge=0)
    missing: tuple[str, ...]
    fault: str

    @model_validator(mode="after")
    def _check(self) -> Self:
        _utc(self.probed_at, "probed_at")
        if self.trigger == "drill" and not self.fault:
            raise ValueError("a drill probe names the file it deleted in fault")
        if self.trigger == "daily" and self.fault:
            raise ValueError("a daily probe deletes nothing: fault must be empty")
        if any(not path for path in self.missing):
            raise ValueError("a missing path is empty")
        if len(self.missing) != len(set(self.missing)):
            raise ValueError("a missing path is listed twice")
        if len(self.missing) > self.expected:
            raise ValueError("more files missing than the head installs")
        return self


class ModelInstallReadingRequest(BaseModel):
    """The probes, the hosts that must have one, the max probe age from the piece's overlay and the time to read at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    required_hosts: tuple[str, ...] = Field(min_length=1)
    probe_max_age_seconds: int = Field(gt=0)
    probes: tuple[ModelInstallProbeFact, ...]
    probes_unread_reason: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _utc(self.now, "now")
        if len(self.required_hosts) != len(set(self.required_hosts)):
            raise ValueError("a required host is listed twice")
        if any(not host for host in self.required_hosts):
            raise ValueError("a required host is empty")
        if (
            self.probes_unread_reason is not None
            and not self.probes_unread_reason.strip()
        ):
            raise ValueError("probes_unread_reason names no reason")
        return self


class ModelInstallReading(BaseModel):
    """The reading at the read time: the verdict, its reasons and how many required hosts read whole."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    hosts_checked: int
    hosts_whole: int
    read_at: AwareDatetime


__all__: list[str] = [
    "ModelInstallProbeFact",
    "ModelInstallReading",
    "ModelInstallReadingRequest",
]
