# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay's ``floor_fill_alarm`` section: where the alarm check reads and writes and the bounds of the rule.

``max_run_age_minutes`` has no default: a fill that never runs (the measured cause) leaves no run for the idle rule to
count, so the binding states how long without a run is an alarm of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from omnimarket.nodes.node_floor_fill_alarm_effect.models.model_floor_fill_alarm_check import (
    TOKEN,
    ModelFloorFillAlarmCheckRequest,
)

ALARM_OVERLAY_SECTION = "floor_fill_alarm"
_PREFIX = f"{ALARM_OVERLAY_SECTION}."


class FloorFillAlarmConfigError(ValueError):
    """The ``floor_fill_alarm`` section is missing keys or holds a value the check refuses; ``keys`` names them."""

    def __init__(self, message: str, keys: tuple[str, ...]) -> None:
        super().__init__(message)
        self.keys = keys


class ModelFloorFillAlarmSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The fill's scope: the key of its state row; its stored answer is ``<scope>:answer``.
    scope: str = Field(min_length=1)
    row_lane: str = Field(pattern=TOKEN)
    row_ticket: str = Field(pattern=TOKEN)
    # The authenticated principal of the host that runs the check (the ledger writer stamps it and refuses another).
    row_principal: str = Field(pattern=TOKEN)
    ledger_path: str = Field(min_length=1)
    # The declared append command, ``{row}`` standing for the row.
    ledger_append_command: tuple[str, ...] = Field(min_length=1)
    run_log_path: str = Field(min_length=1)
    # One line per check, written whether the check found nothing, wrote rows or failed on a source it could not read.
    check_log_path: str = Field(min_length=1)
    # The pool read of the check itself: the same lane host pool snapshot the fill reads, refused when stale.
    pool_snapshot_path: str = Field(min_length=1)
    pool_record_suffix: str = Field(min_length=1)
    pool_snapshot_max_age_seconds: int = Field(gt=0)
    pool_engine: str = Field(min_length=1)
    max_run_age_minutes: int = Field(ge=1)
    repeat_after_minutes: int = Field(default=60, ge=1)
    slot_threshold: int = Field(default=5, ge=0)
    idle_runs: int = Field(default=2, ge=1)
    claim_deadline_minutes: int = Field(default=10, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        if isinstance(data, dict):
            required = [
                name for name, field in cls.model_fields.items() if field.is_required()
            ]
            missing = [name for name in required if data.get(name) is None]
            if missing:
                raise ValueError(
                    "no overlay supplies " + ", ".join(f"{_PREFIX}{n}" for n in missing)
                )
        return data

    @model_validator(mode="after")
    def _append_command_carries_the_row(self) -> ModelFloorFillAlarmSettings:
        if not any("{row}" in part for part in self.ledger_append_command):
            raise ValueError(
                f"{_PREFIX}ledger_append_command carries no {{row}} element"
            )
        return self

    def check_request(self, now: datetime) -> ModelFloorFillAlarmCheckRequest:
        """The request of one check at ``now``, every bound taken from these settings."""
        return ModelFloorFillAlarmCheckRequest(
            now=now,
            row_lane=self.row_lane,
            row_ticket=self.row_ticket,
            row_principal=self.row_principal,
            repeat_after_minutes=self.repeat_after_minutes,
            slot_threshold=self.slot_threshold,
            idle_runs=self.idle_runs,
            claim_deadline_minutes=self.claim_deadline_minutes,
            max_run_age_minutes=self.max_run_age_minutes,
        )


def load_floor_fill_alarm_settings(
    section: Mapping[str, Any],
) -> ModelFloorFillAlarmSettings:
    """Build the settings from the overlay's ``floor_fill_alarm`` mapping, refusing naming the keys."""
    try:
        return ModelFloorFillAlarmSettings.model_validate(dict(section))
    except ValidationError as error:
        text = "; ".join(str(item["msg"]) for item in error.errors())
        located = [
            f"{_PREFIX}{'.'.join(str(p) for p in item['loc'])}"
            for item in error.errors()
            if item["loc"]
        ]
        named = [
            token.rstrip(",;)(")
            for token in text.replace(",", " ").split()
            if token.startswith(_PREFIX)
        ]
        raise FloorFillAlarmConfigError(
            text, tuple(dict.fromkeys([*located, *named]))
        ) from error


__all__: list[str] = [
    "ALARM_OVERLAY_SECTION",
    "FloorFillAlarmConfigError",
    "ModelFloorFillAlarmSettings",
    "load_floor_fill_alarm_settings",
]
