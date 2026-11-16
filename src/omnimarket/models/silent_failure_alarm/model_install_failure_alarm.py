# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models of the repeating install-failure alarm decision (OMN-21663).

The decision is pure: the caller reads the install-failure jsonl and the persisted state, and
passes the rows and the previous state in. The handler counts the failures inside the window and
says whether to raise, hold, or forget the last raise (OMN-21663).
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

__all__ = [
    "ModelInstallFailureAlarmRequest",
    "ModelInstallFailureAlarmResult",
    "ModelInstallFailureRow",
    "ModelInstallFailureState",
]


class ModelInstallFailureRow(BaseModel):
    """One install-failure record the caller read from the jsonl (OMN-21663)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: AwareDatetime
    detail: str
    failure_class: str = ""


class ModelInstallFailureState(BaseModel):
    """The persisted state of the install-failure alarm (OMN-21663)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    raised_at: AwareDatetime | None = None
    raise_count: int = Field(default=0, ge=0)


class ModelInstallFailureAlarmRequest(BaseModel):
    """The facts the caller read, for the install-failure decision (OMN-21663)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    readable: bool
    failures: tuple[ModelInstallFailureRow, ...] = ()
    threshold: int = Field(gt=0)
    window_minutes: int = Field(gt=0)
    reraise_after_minutes: int = Field(ge=0)
    previous: ModelInstallFailureState = ModelInstallFailureState()


class ModelInstallFailureAlarmResult(BaseModel):
    """The install-failure decision and the state to persist (OMN-21663)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["OK", "RAISE", "HELD", "UNREADABLE"]
    count: int
    raise_count: int
    alert_text: tuple[str, ...] = ()
    state: ModelInstallFailureState
