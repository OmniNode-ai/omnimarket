# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of piece 11's reading (OMN-22958): merges, lab pass receipts, probes and the verdict.

The receipt fact is the part of ``ModelLabPassReceipt`` (omnibase_infra, ``scripts/ci/lab_pass_receipt.py``)
the reading needs: the exact sha, the lane, the result and when it finished. Every sha is 40 lowercase
hex, because a receipt for a descendant or an abbreviation is not a receipt for the merged sha.
"""

from __future__ import annotations

import re
from datetime import UTC, timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict

_SHA = re.compile(r"^[0-9a-f]{40}$")


def _exact_sha(value: str) -> str:
    if not _SHA.match(value):
        raise ValueError(f"not an exact sha (40 lowercase hex): {value!r}")
    return value


def _utc(value: AwareDatetime, field: str) -> AwareDatetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} is not UTC: {value.isoformat()}")
    return value.astimezone(UTC)


class ModelRuntimeMerge(BaseModel):
    """One merge to the dev branch. Only a runtime-affecting merge needs a lab pass receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    merged_at: AwareDatetime
    runtime_affecting: bool

    @model_validator(mode="after")
    def _check(self) -> Self:
        _exact_sha(self.sha)
        _utc(self.merged_at, "merged_at")
        return self


class ModelLabPassReceiptFact(BaseModel):
    """What a lab pass receipt says: the exact sha it covers, its lane, its result and when it finished."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    lane: str = Field(min_length=1)
    result: Literal["PASS", "FAIL"]
    finished_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        _exact_sha(self.sha)
        _utc(self.finished_at, "finished_at")
        return self


class ModelRuntimeProbeFact(BaseModel):
    """One functional probe of the running runtime: the sha it was asked for and the sha the runtime reported."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    probed_at: AwareDatetime
    expected_sha: str
    observed_sha: str | None
    ok: bool

    @model_validator(mode="after")
    def _check(self) -> Self:
        _exact_sha(self.expected_sha)
        if self.observed_sha is not None:
            _exact_sha(self.observed_sha)
        _utc(self.probed_at, "probed_at")
        return self


class ModelDevDeployReadingRequest(BaseModel):
    """The facts, the thresholds from the piece's overlay and the time to read at. Nothing has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    required_lane: str = Field(min_length=1)
    receipt_deadline_seconds: int = Field(gt=0)
    probe_max_age_seconds: int = Field(gt=0)
    merges: tuple[ModelRuntimeMerge, ...]
    receipts: tuple[ModelLabPassReceiptFact, ...]
    probes: tuple[ModelRuntimeProbeFact, ...]
    receipts_unread_reason: str | None
    probes_unread_reason: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _utc(self.now, "now")
        shas = [m.sha for m in self.merges]
        if len(shas) != len(set(shas)):
            raise ValueError("a merge sha is listed twice")
        for name in ("receipts_unread_reason", "probes_unread_reason"):
            reason = getattr(self, name)
            if reason is not None and not reason.strip():
                raise ValueError(f"{name} names no reason")
        return self


class ModelDevDeployReading(BaseModel):
    """The reading at the read time. ``pending`` lists merges still inside their deadline; they fail nothing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumReadingVerdict
    reasons: tuple[str, ...]
    pending: tuple[str, ...]
    merges_checked: int
    merges_receipted: int
    probe_age_seconds: int | None
    read_at: AwareDatetime


__all__: list[str] = [
    "ModelDevDeployReading",
    "ModelDevDeployReadingRequest",
    "ModelLabPassReceiptFact",
    "ModelRuntimeMerge",
    "ModelRuntimeProbeFact",
]
