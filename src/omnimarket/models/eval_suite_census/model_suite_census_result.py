# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census answer: band, repository, and control counts (OMN-23883)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.eval_suite_census.enum_suite_census_failure import (
    EnumSuiteCensusFailure,
)


class ModelCodeEvalSuiteCandidate(BaseModel):
    """One admitted candidate row the trial driver's run request takes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    merge_commit: str
    contract_id: str


class ModelCodeEvalBandCount(BaseModel):
    """How many admitted tasks a band holds against the line's required n."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: EnumSizeBand
    admitted: int = Field(ge=0)
    required_cell_n: int = Field(ge=1)
    cell_shortfall: int = Field(ge=0)
    required_paired_n: int = Field(ge=1)
    paired_shortfall: int = Field(ge=0)


class ModelCodeEvalRepositoryCount(BaseModel):
    """How many admitted tasks a repository holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    admitted: int = Field(ge=0)


class ModelCodeEvalBandRepositoryCount(BaseModel):
    """How many admitted tasks a (band, repository) pair holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    band: EnumSizeBand
    repository: str
    admitted: int = Field(ge=0)


class ModelCodeEvalReasonCount(BaseModel):
    """How many candidates a single exclusion reason dropped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    count: int = Field(ge=1)


class ModelCodeEvalControlCounts(BaseModel):
    """The control verdicts over every task in the request.

    ``holds`` counts a pass control; ``does_not_hold`` counts a fail verdict or
    an unscorable control whose source is falsifier_not_held; ``could_not_run``
    counts any other unscorable control.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    holds: int = Field(ge=0)
    does_not_hold: int = Field(ge=0)
    could_not_run: int = Field(ge=0)


class ModelCodeEvalSuiteCensus(BaseModel):
    """The census answer: counts, candidates, and the typed failure if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: str
    suite_digest: str
    candidates_total: int = Field(ge=0)
    control: ModelCodeEvalControlCounts | None = None
    by_band: tuple[ModelCodeEvalBandCount, ...] = ()
    by_repository: tuple[ModelCodeEvalRepositoryCount, ...] = ()
    by_band_repository: tuple[ModelCodeEvalBandRepositoryCount, ...] = ()
    excluded: tuple[ModelCodeEvalReasonCount, ...] = ()
    candidates: tuple[ModelCodeEvalSuiteCandidate, ...] = ()
    failure: EnumSuiteCensusFailure | None = None
    detail: str = ""


__all__ = [
    "ModelCodeEvalBandCount",
    "ModelCodeEvalBandRepositoryCount",
    "ModelCodeEvalControlCounts",
    "ModelCodeEvalReasonCount",
    "ModelCodeEvalRepositoryCount",
    "ModelCodeEvalSuiteCandidate",
    "ModelCodeEvalSuiteCensus",
]
