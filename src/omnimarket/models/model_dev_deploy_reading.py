# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of piece 11's reading (OMN-22958): merges, lab pass receipts, probes and the verdict.

The receipt fact is the part of ``ModelLabPassReceipt`` (omnibase_infra, ``scripts/ci/lab_pass_receipt.py``)
the reading needs: the exact sha, the lane, the result and when it finished. Every sha is 40 lowercase
hex. A receipt for a descendant counts for an earlier merge only through ``ModelLineageFact``: the clone's own
reading of ancestry between two exact shas, judged under the overlay's ``ModelLineageRule`` (ruling 2026-11-10T16:53:27Z).
"""

from __future__ import annotations

import re
from datetime import UTC, timedelta
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.model_reading import EnumReadingVerdict

_SHA = re.compile(r"[0-9a-f]{40}")


def _exact_sha(value: str) -> str:
    if not _SHA.fullmatch(value):
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


class ModelLineageRule(BaseModel):
    """The overlay's rule and its bounds for a later commit standing in for an earlier one. Nothing has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: A PASS receipt whose sha has a runtime-affecting merge as an ancestor covers that merge.
    receipt_covers_descendant: bool
    #: A running sha that descends from the expected one passes when no runtime-affecting commit lies between.
    probe_accepts_descendant: bool
    #: The most first-parent commits that may lie between the earlier sha and the later one.
    max_commits_between: int = Field(gt=0)
    #: How long after the merge a covering receipt may have finished.
    receipt_cover_window_seconds: int = Field(gt=0)


class ModelLineageFact(BaseModel):
    """What the clone says of ``base_sha`` and ``head_sha``: ancestor, not an ancestor, or unread (never a pass)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_sha: str
    head_sha: str
    relation: Literal["ancestor", "not-ancestor", "unread"]
    #: First-parent commits in ``base..head``; read only when ``relation`` is ancestor.
    commits_between: int | None = Field(default=None, ge=0)
    #: Runtime-affecting commits in ``base..head``; read only for a probe's pair, empty otherwise.
    runtime_affecting_between: tuple[str, ...] = ()
    unread_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _exact_sha(self.base_sha)
        _exact_sha(self.head_sha)
        for sha in self.runtime_affecting_between:
            _exact_sha(sha)
        if self.relation == "ancestor":
            if self.commits_between is None or self.unread_reason is not None:
                raise ValueError(
                    "an ancestor reads its commit count and no unread reason"
                )
        elif self.commits_between is not None or self.runtime_affecting_between:
            raise ValueError(
                f"a {self.relation} pair carries no commit count or runtime commits"
            )
        if self.relation == "unread" and not (self.unread_reason or "").strip():
            raise ValueError("an unread pair names its reason")
        if self.relation != "unread" and self.unread_reason is not None:
            raise ValueError("only an unread pair carries an unread reason")
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
    lineage_rule: ModelLineageRule
    lineage: tuple[ModelLineageFact, ...]
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
    "ModelLineageFact",
    "ModelLineageRule",
    "ModelRuntimeMerge",
    "ModelRuntimeProbeFact",
]
