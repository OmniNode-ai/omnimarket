# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed settings, requests, results and alarm of the nightly miss collector."""

from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from omnimarket.models.test_selector_reading import (
    EnumFileOutcome,
    ModelNightlyAttribution,
)

POLICY_PATH = Path(__file__).resolve().parents[1] / "policy.yaml"
ANY_FILE = "*"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OwnerRoleUnsetError(ValueError):
    """The overlay names no owner role, so an alarm would reach nobody: the effect refuses to start."""

    code = "owner_role_unset"


class EnumAlarmKind(StrEnum):
    MISS = "miss"
    UNATTRIBUTABLE = "unattributable"
    INCOMPLETE = "incomplete"
    NO_COLLECTOR_RESULT = "no_collector_result"


class EnumCollectStatus(StrEnum):
    DISABLED = "disabled"
    NO_RUN = "no_run"
    WAITING = "waiting"
    READ_FAILED = "read_failed"
    COLLECTED = "collected"


class EnumSlotStatus(StrEnum):
    RESULT_RECORDED = "result_recorded"
    WITHIN_LAG = "within_lag"
    ALARMED = "alarmed"


class ModelMissCollectPolicy(_Frozen):
    nightly_shard_count: int = Field(ge=1)
    nightly_min_test_files: int = Field(ge=0)
    collector_max_lag_hours: int = Field(ge=1)
    nightly_slot_time_utc: time
    record_retention_days: int = Field(ge=1)

    @field_validator("nightly_slot_time_utc", mode="before")
    @classmethod
    def _slot_time(cls, value: object) -> object:
        if isinstance(value, str):
            hour, _, minute = value.partition(":")
            return time(int(hour), int(minute))
        return value


def load_policy(path: Path = POLICY_PATH) -> ModelMissCollectPolicy:
    return ModelMissCollectPolicy.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


class ModelMissCollectSettings(_Frozen):
    """The deployment overlay. ``owner_role`` has no default."""

    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    workflow_file: str = Field(min_length=1)
    owner_role: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    alarm_route: str = Field(min_length=1)
    collector_enabled: bool
    status_tick: str = Field(min_length=1)

    @classmethod
    def load(cls, document: dict[str, Any]) -> Self:
        """Validate the overlay; an unset or blank owner role is the typed OwnerRoleUnsetError."""
        owner = document.get("owner_role")
        if not isinstance(owner, str) or not owner.strip():
            raise OwnerRoleUnsetError(
                "the overlay declares no owner_role; refusing to start"
            )
        try:
            return cls.model_validate(document)
        except ValidationError as exc:
            if any(e["loc"] == ("owner_role",) for e in exc.errors()):
                raise OwnerRoleUnsetError(
                    "the overlay's owner_role is not a role; refusing to start"
                ) from exc
            raise


class ModelNightlyRun(_Frozen):
    run_id: int = Field(ge=1)
    completed: bool
    head_sha: str = Field(min_length=1)


class ModelNightReading(_Frozen):
    """What the gateway read of one night's artifacts."""

    artifacts_found: int = Field(ge=0)
    shards_in_error: int = Field(ge=0)
    outcomes: dict[str, EnumFileOutcome]


class ModelPreviousNight(_Frozen):
    head_sha: str = Field(min_length=1)
    outcomes: dict[str, EnumFileOutcome]


class ModelNightlyAlarm(_Frozen):
    """One alarm, keyed (repo, nightly slot date, test file or ``*``)."""

    repo: str
    slot_date: date
    test_file: str = Field(min_length=1)
    kind: EnumAlarmKind
    owner_role: str
    route: str
    text: str = Field(min_length=1)

    @property
    def key(self) -> str:
        return f"{self.repo}|{self.slot_date.isoformat()}|{self.test_file}"


class ModelCollectRequest(_Frozen):
    slot_date: date
    now: AwareDatetime


class ModelSlotCheckRequest(_Frozen):
    slot_date: date
    now: AwareDatetime


class ModelCollectResult(_Frozen):
    status: EnumCollectStatus
    slot_date: date
    attribution: ModelNightlyAttribution | None = None
    #: Alarm keys this invocation created.
    alarms_created: tuple[str, ...] = ()
    #: Alarm keys the receiver took in this invocation (new creations and retried pending ones).
    alarms_delivered: tuple[str, ...] = ()
    #: Alarms that stay pending after a delivery failure; the next invocation retries them.
    delivery_failures: tuple[str, ...] = ()
    failure: str = ""


class ModelSlotCheckResult(_Frozen):
    status: EnumSlotStatus
    slot_date: date
    deadline: datetime
    alarms_created: tuple[str, ...] = ()
    alarms_delivered: tuple[str, ...] = ()
    delivery_failures: tuple[str, ...] = ()
    #: UNKNOWN for the night while no collector result exists, so the reading node's state read agrees.
    reading_state: str = ""
