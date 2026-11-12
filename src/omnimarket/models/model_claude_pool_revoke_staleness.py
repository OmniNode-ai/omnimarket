# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: models for the Claude token pool revoke staleness decision."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


def _require_tz_aware(value: datetime, info: ValidationInfo) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{info.field_name} must be timezone-aware")
    return value


class ModelRevokeObservation(BaseModel):
    """One success of a token on a host, after the revoke it may clear."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    kind: Literal["limited", "ok"]
    at: datetime
    fingerprint: str = Field(min_length=1)

    @field_validator("at")
    @classmethod
    def _at_must_be_tz_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return _require_tz_aware(value, info)


class ModelRevokeStalenessPolicy(BaseModel):
    """The margin after a revoke before a later success counts as evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    margin_seconds: int = Field(ge=0)


class ModelClearRevokeCommand(BaseModel):
    """The clear to issue when a revoke is proven stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    fingerprint: str
    expected_revoked_at: datetime
    evidence_host: str
    evidence_kind: Literal["limited", "ok"]
    evidence_at: datetime

    @field_validator("expected_revoked_at")
    @classmethod
    def _expected_revoked_at_must_be_tz_aware(
        cls, value: datetime, info: ValidationInfo
    ) -> datetime:
        return _require_tz_aware(value, info)

    @field_validator("evidence_at")
    @classmethod
    def _evidence_at_must_be_tz_aware(
        cls, value: datetime, info: ValidationInfo
    ) -> datetime:
        return _require_tz_aware(value, info)


class ModelRevokeStalenessRequest(BaseModel):
    """One revoke to judge, with the observations and the policy margin."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    revoked_at: datetime | None = None
    observations: tuple[ModelRevokeObservation, ...] = ()
    policy: ModelRevokeStalenessPolicy | None = None

    @field_validator("revoked_at")
    @classmethod
    def _revoked_at_must_be_tz_aware_when_set(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        if value is not None:
            return _require_tz_aware(value, info)
        return value


class ModelRevokeStalenessResult(BaseModel):
    """The verdict for one revoke, with the clear command when it is stale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    verdict: Literal["stale", "real", "unknown", "not_configured"]
    reason: str
    clear: ModelClearRevokeCommand | None = None

    @model_validator(mode="after")
    def _clear_is_set_iff_stale(self) -> Self:
        if self.verdict == "stale" and self.clear is None:
            raise ValueError("clear must be set when verdict is stale")
        if self.verdict != "stale" and self.clear is not None:
            raise ValueError("clear must be None when verdict is not stale")
        return self
