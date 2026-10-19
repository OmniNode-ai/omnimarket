# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelReviewTargetSnapshot — the immutable target a review was read against.

Owner module (OMN-21189). The diff effect returns one snapshot per resolved pull
request diff: the head sha, the base sha and the digest of the diff text, read
together. When the three cannot be read together (a push landed between the
reads, or a read failed) the snapshot is ``unavailable`` and carries the reason;
no field is filled in from a later read.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumReviewSnapshotStatus(StrEnum):
    ESTABLISHED = "established"
    UNAVAILABLE = "unavailable"


def diff_digest(diff_text: str) -> str:
    """Return the ``sha256:<hex>`` digest of a diff's UTF-8 text."""
    return "sha256:" + hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


class ModelReviewTargetSnapshot(BaseModel):
    """Head sha, base sha and diff digest read together, or unavailable with a reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumReviewSnapshotStatus = Field(...)
    head_sha: str | None = Field(default=None)
    base_sha: str | None = Field(default=None)
    diff_digest: str | None = Field(default=None)
    unavailable_reason: str | None = Field(default=None)

    @model_validator(mode="after")
    def _fields_match_status(self) -> Self:
        fields = (self.head_sha, self.base_sha, self.diff_digest)
        if self.status is EnumReviewSnapshotStatus.ESTABLISHED:
            if any(not value for value in fields):
                raise ValueError(
                    "an established snapshot carries head_sha, base_sha and diff_digest"
                )
            if self.unavailable_reason is not None:
                raise ValueError(
                    "an established snapshot carries no unavailable_reason"
                )
        else:
            if not self.unavailable_reason:
                raise ValueError("an unavailable snapshot carries a reason")
            if any(value is not None for value in fields):
                raise ValueError(
                    "an unavailable snapshot carries no sha or digest; a value "
                    "from a later read would not describe the reviewed target"
                )
        return self

    @classmethod
    def established(cls, *, head_sha: str, base_sha: str, diff_text: str) -> Self:
        return cls(
            status=EnumReviewSnapshotStatus.ESTABLISHED,
            head_sha=head_sha,
            base_sha=base_sha,
            diff_digest=diff_digest(diff_text),
        )

    @classmethod
    def unavailable(cls, reason: str) -> Self:
        return cls(
            status=EnumReviewSnapshotStatus.UNAVAILABLE, unavailable_reason=reason
        )


__all__: list[str] = [
    "EnumReviewSnapshotStatus",
    "ModelReviewTargetSnapshot",
    "diff_digest",
]
