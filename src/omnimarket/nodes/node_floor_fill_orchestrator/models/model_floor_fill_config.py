# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay keys the orchestrator reads (``floor_fill.*``). No default: a missing key refuses naming it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

OVERLAY_PREFIX = "floor_fill."
SCRIPT_RUNNER_NAME = "onex_remote_lane"
ARGV_PLACEHOLDER = "{argv}"


class FloorFillConfigError(ValueError):
    """The overlay is refused; ``keys`` names every overlay key the refusal is about."""

    def __init__(self, message: str, keys: tuple[str, ...]) -> None:
        super().__init__(message)
        self.keys = keys


class ModelFloorFillConfig(BaseModel):
    """The orchestrator's overlay keys. Every one is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(min_length=1)
    host_id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    plans_root: str = Field(min_length=1)
    runner_command: tuple[str, ...] = Field(min_length=1)
    reserved_timeout_seconds: int = Field(gt=0)
    sent_window_minutes: int = Field(gt=0)
    command_timeout_seconds: int = Field(gt=0)
    tick_interval_seconds: int = Field(gt=0)
    remote_argv: tuple[str, ...] = Field(min_length=1)
    process_environment: dict[str, str]
    process_working_directory: str = Field(min_length=1)
    max_age_minutes: int = Field(gt=0)
    max_fill_lanes_per_line: int = Field(ge=1)
    stopped_max_lanes: int = Field(ge=1)
    stopped_report_chars: int = Field(gt=0)
    stopped_continue_rule: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        if isinstance(data, dict):
            missing = [name for name in cls.model_fields if data.get(name) is None]
            if missing:
                keys = tuple(f"{OVERLAY_PREFIX}{name}" for name in missing)
                raise ValueError("no overlay supplies " + ", ".join(keys))
        return data

    @model_validator(mode="after")
    def _the_timeouts_are_ordered(self) -> Self:
        if self.command_timeout_seconds >= self.reserved_timeout_seconds:
            raise ValueError(
                f"{OVERLAY_PREFIX}command_timeout_seconds ({self.command_timeout_seconds}) "
                f"is not below {OVERLAY_PREFIX}reserved_timeout_seconds "
                f"({self.reserved_timeout_seconds})"
            )
        if self.tick_interval_seconds >= self.max_age_minutes * 60:
            raise ValueError(
                f"{OVERLAY_PREFIX}tick_interval_seconds ({self.tick_interval_seconds}) "
                f"is not below {OVERLAY_PREFIX}max_age_minutes ({self.max_age_minutes}) in seconds"
            )
        return self

    @model_validator(mode="after")
    def _no_script_runner(self) -> Self:
        for key, argv in (
            ("runner_command", self.runner_command),
            ("remote_argv", self.remote_argv),
        ):
            if any(SCRIPT_RUNNER_NAME in element for element in argv):
                raise ValueError(
                    f"{OVERLAY_PREFIX}{key} names the script runner; "
                    "the fill starts the lane through the node entry"
                )
        if ARGV_PLACEHOLDER not in self.remote_argv:
            raise ValueError(
                f"{OVERLAY_PREFIX}remote_argv carries no {ARGV_PLACEHOLDER} element"
            )
        return self

    def check_fill_host(self, hosts: Iterable[tuple[str, bool]]) -> None:
        """``host_id`` must be an available row of the lab host table: ``(name, available)`` pairs."""
        available = dict(hosts)
        if self.host_id not in available:
            raise FloorFillConfigError(
                f"{OVERLAY_PREFIX}host_id {self.host_id!r} is not a row of the lab host table",
                (f"{OVERLAY_PREFIX}host_id",),
            )
        if not available[self.host_id]:
            raise FloorFillConfigError(
                f"{OVERLAY_PREFIX}host_id {self.host_id!r} is not available for lane work",
                (f"{OVERLAY_PREFIX}host_id",),
            )


def load_floor_fill_config(overlay: Mapping[str, Any]) -> ModelFloorFillConfig:
    """Build the config from the overlay's ``floor_fill`` mapping, refusing naming the keys."""
    try:
        return ModelFloorFillConfig.model_validate(dict(overlay))
    except ValidationError as error:
        messages = [str(item["msg"]) for item in error.errors()]
        text = "; ".join(messages)
        located = [
            f"{OVERLAY_PREFIX}{'.'.join(str(part) for part in item['loc'])}"
            for item in error.errors()
            if item["loc"]
        ]
        keys = tuple(
            dict.fromkeys(
                [
                    *located,
                    *(
                        token.rstrip(",;)(")
                        for token in text.replace(",", " ").split()
                        if token.startswith(OVERLAY_PREFIX)
                    ),
                ]
            )
        )
        raise FloorFillConfigError(text, keys) from error


__all__: list[str] = [
    "ARGV_PLACEHOLDER",
    "OVERLAY_PREFIX",
    "FloorFillConfigError",
    "ModelFloorFillConfig",
    "load_floor_fill_config",
]
