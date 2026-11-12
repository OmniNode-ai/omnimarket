# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23887: models for the Claude token pool limit recheck decision."""

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


def _require_tz_aware_when_set(
    value: datetime | None, info: ValidationInfo
) -> datetime | None:
    if value is not None:
        return _require_tz_aware(value, info)
    return value


class ModelLimitRecheckPolicy(BaseModel):
    """The minimum spacing between two probes of the same limited label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_seconds: int = Field(ge=60)


class ModelLimitProbeAnswer(BaseModel):
    """What a probe of a limited label answered, with its reset time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["ok", "limited", "error"]
    reset_at: datetime | None = None
    probed_at: datetime

    @field_validator("reset_at")
    @classmethod
    def _reset_at_must_be_tz_aware_when_set(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return _require_tz_aware_when_set(value, info)

    @field_validator("probed_at")
    @classmethod
    def _probed_at_must_be_tz_aware(
        cls, value: datetime, info: ValidationInfo
    ) -> datetime:
        return _require_tz_aware(value, info)


class ModelLimitRecheckCommand(BaseModel):
    """What to do with the limited-until mark once a probe answered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    fingerprint: str
    expected_until: datetime
    action: Literal["clear", "replace", "keep"]
    new_until: datetime | None = None
    probed_at: datetime

    @field_validator("expected_until")
    @classmethod
    def _expected_until_must_be_tz_aware(
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
    def _new_until_is_set_iff_replace(self) -> Self:
        if self.action == "replace" and self.new_until is None:
            raise ValueError("new_until must be set when action is replace")
        if self.action != "replace" and self.new_until is not None:
            raise ValueError("new_until must be None when action is not replace")
        return self


class ModelLimitRecheckRequest(BaseModel):
    """One limited label to judge, with its mark, its clock and any answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    until: datetime | None = None
    last_probe_at: datetime | None = None
    now: datetime
    policy: ModelLimitRecheckPolicy | None = None
    probe: ModelLimitProbeAnswer | None = None

    @field_validator("until")
    @classmethod
    def _until_must_be_tz_aware_when_set(
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


class ModelLimitRecheckResult(BaseModel):
    """The decision for one limited label, with the command when one is issued."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    action: Literal[
        "not_configured",
        "not_limited",
        "not_due",
        "probe",
        "clear",
        "replace",
        "keep",
    ]
    reason: str
    command: ModelLimitRecheckCommand | None = None

    @model_validator(mode="after")
    def _command_matches_the_action(self) -> Self:
        # "keep" may carry a command (a limited answer whose probe time is
        # recorded) or not (an error answer proves nothing); the rules decide.
        if self.action in ("clear", "replace") and self.command is None:
            raise ValueError(f"command must be set when action is {self.action}")
        if (
            self.action in ("not_configured", "not_limited", "not_due", "probe")
            and self.command is not None
        ):
            raise ValueError(f"command must be None when action is {self.action}")
        return self
