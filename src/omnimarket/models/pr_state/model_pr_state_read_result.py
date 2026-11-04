# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed results of reading a pull request's state from the projection.

Every outcome is a returned value. A row too old to be current is a ``STALE``
result that names its age and ``observed_at`` and carries none of the row's
contents, so it cannot be mistaken for current data.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.pr_state.model_pr_state_row import ModelPrStateRow


class EnumPrStateReadOutcome(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    ABSENT = "ABSENT"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


class ModelPrStateReadFresh(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[EnumPrStateReadOutcome.FRESH] = EnumPrStateReadOutcome.FRESH
    row: ModelPrStateRow
    row_age_seconds: float


class ModelPrStateReadStale(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[EnumPrStateReadOutcome.STALE] = EnumPrStateReadOutcome.STALE
    repo: str
    pr_number: int
    observed_at: datetime
    row_age_seconds: float
    max_row_age_seconds: int


class ModelPrStateReadAbsent(BaseModel):
    """No row for the pull request in the page.

    ``window_truncated`` is true when the exposure holds rows the page left out,
    so the projection may still hold an older row for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[EnumPrStateReadOutcome.ABSENT] = EnumPrStateReadOutcome.ABSENT
    repo: str
    pr_number: int
    window_truncated: bool


class ModelPrStateReadTimeout(BaseModel):
    """The wait ended before a fresh row reached a terminal verdict.

    ``last_observed_at`` and ``last_head_sha`` describe the newest matching row
    seen, if any.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[EnumPrStateReadOutcome.TIMEOUT] = EnumPrStateReadOutcome.TIMEOUT
    repo: str
    pr_number: int
    head_sha: str | None
    waited_seconds: float
    timeout_seconds: float
    last_observed_at: datetime | None
    last_head_sha: str | None


class ModelPrStateReadUnavailable(BaseModel):
    """The projection did not serve a page; ``error`` is its fixed code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[EnumPrStateReadOutcome.UNAVAILABLE] = (
        EnumPrStateReadOutcome.UNAVAILABLE
    )
    status_code: int
    error: str


ModelPrStateReadOutcome = Annotated[
    ModelPrStateReadFresh
    | ModelPrStateReadStale
    | ModelPrStateReadAbsent
    | ModelPrStateReadTimeout
    | ModelPrStateReadUnavailable,
    Field(discriminator="outcome"),
]


class ModelPrStateReadResult(BaseModel):
    """The answer for one pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: ModelPrStateReadOutcome
