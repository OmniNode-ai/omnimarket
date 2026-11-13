# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: models for the Claude token pool revoke recheck decision."""

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

from omnimarket.models.model_claude_pool_limit_recheck import ModelLimitProbeAnswer


def _require_tz_aware(value: datetime, info: ValidationInfo) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{info.field_name} must be timezone-aware")
    return value


def _require_tz_aware_when_set(
    value: datetime | None, info: ValidationInfo
) -> datetime | None:
    if value is not None:
        return _require_tz_aware(value, info)
    return value


class ModelRevokeRecheckPolicy(BaseModel):
    """The minimum spacing between two probes of the same revoked label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_seconds: int = Field(ge=60)


class ModelRevokeRecheckRequest(BaseModel):
    """One revoked label to judge, with its mark, its clock and any answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    cause: Literal["revoked", "challenged"]
    revoked_at: datetime | None = None
    last_probe_at: datetime | None = None
    now: datetime
    policy: ModelRevokeRecheckPolicy | None = None
    probe: ModelLimitProbeAnswer | None = None

    @field_validator("revoked_at")
    @classmethod
    def _revoked_at_must_be_tz_aware_when_set(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return _require_tz_aware_when_set(value, info)

    @field_validator("last_probe_at")
    @classmethod
    def _last_probe_at_must_be_tz_aware_when_set(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return _require_tz_aware_when_set(value, info)

    @field_validator("now")
    @classmethod
    def _now_must_be_tz_aware(cls, value: datetime, info: ValidationInfo) -> datetime:
        return _require_tz_aware(value, info)


class ModelRevokeRecheckCommand(BaseModel):
    """What to do with the revoked mark once a probe answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    fingerprint: str
    expected_revoked_at: datetime
    action: Literal["clear", "confirm", "keep"]
    new_until: datetime | None = None
    probed_at: datetime

    @field_validator("expected_revoked_at")
    @classmethod
    def _expected_revoked_at_must_be_tz_aware(
        cls, value: datetime, info: ValidationInfo
    ) -> datetime:
        return _require_tz_aware(value, info)

    @field_validator("new_until")
    @classmethod
    def _new_until_must_be_tz_aware_when_set(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return _require_tz_aware_when_set(value, info)

    @field_validator("probed_at")
    @classmethod
    def _probed_at_must_be_tz_aware(
        cls, value: datetime, info: ValidationInfo
    ) -> datetime:
        return _require_tz_aware(value, info)

    @model_validator(mode="after")
    def _new_until_is_set_only_when_clear(self) -> Self:
        if self.action != "clear" and self.new_until is not None:
            raise ValueError("new_until must be None when action is not clear")
        return self


class ModelRevokeRecheckResult(BaseModel):
    """The decision for one revoked label, with the command when one is issued."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    action: Literal[
        "not_configured",
        "not_revoked",
        "not_due",
        "probe",
        "clear",
        "confirm",
        "keep",
    ]
    reason: str
    command: ModelRevokeRecheckCommand | None = None

    @model_validator(mode="after")
    def _command_matches_the_action(self) -> Self:
        # "keep" may carry a command (a refused answer whose probe time is
        # recorded) or not (an error answer proves nothing); the rules decide.
        if self.action in ("clear", "confirm") and self.command is None:
            raise ValueError(f"command must be set when action is {self.action}")
        if (
            self.action in ("not_configured", "not_revoked", "not_due", "probe")
            and self.command is not None
        ):
            raise ValueError(f"command must be None when action is {self.action}")
        return self
