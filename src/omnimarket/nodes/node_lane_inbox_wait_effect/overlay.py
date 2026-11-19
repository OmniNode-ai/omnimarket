# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lane_inbox_wait_effect: the wait's budget, its poll interval and
what counts as binding (why-process-6803).

Found at ``<root>/node_lane_inbox_wait_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` (the in-memory
backend, synthetic values). Nothing here has a default: an absent key refuses the load
and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_lane_inbox_wait_effect"


class ModelReaderSettings(BaseModel):
    """How the runtime backend reads a lane's inbox: one subprocess read per poll."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The environment variable holding the interpreter command (possibly with flags)
    #: the reader module runs under; its first token is the executable.
    interpreter_env: str = Field(min_length=1)
    reader_module: str = Field(min_length=1)
    reader_name: str = Field(min_length=1)
    #: How many hours of the ledger the reader returns each poll.
    window_hours: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    #: The reader's exit codes that mean the read answered; anything else is unreadable.
    ok_exits: tuple[int, ...] = Field(min_length=1)
    #: The label a failed read reports, so a caller can tell reader failures apart.
    unreadable_label: str = Field(min_length=1)


class ModelLaneInboxWaitOverlay(BaseModel):
    """Every knob of one wait; the deployment sets them all."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` reads scripted answers on a fake clock; ``runtime`` reads the lane's
    #: inbox through the reader module in a subprocess and sleeps for real.
    backend: Literal["inmemory", "runtime"]
    #: How long the wait sleeps between two reads of the inbox.
    poll_interval_seconds: float = Field(gt=0)
    #: The longest budget one command may ask for.
    max_wait_seconds: float = Field(gt=0)
    #: The row types that end the wait when they are addressed to the lane.
    binding_types: tuple[str, ...] = Field(min_length=1)
    #: The ``to=`` target that reaches every lane.
    all_target: str = Field(min_length=1)
    #: A binding MSG carrying ``<stop_cell>=<stop_value>`` is a stop order.
    stop_cell: str = Field(min_length=1)
    stop_value: str = Field(min_length=1)
    #: A HOLD carrying any of these cells is skipped unless it names the lane in ``to=``.
    skip_hold_cells: tuple[str, ...]
    #: The most rows one result lists; ``total`` still counts them all.
    max_listed: int = Field(ge=1)
    reader: ModelReaderSettings

    @field_validator("binding_types", "skip_hold_cells")
    @classmethod
    def _non_empty_cells(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item:
                raise ValueError("every entry must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _poll_within_budget(self) -> ModelLaneInboxWaitOverlay:
        if self.poll_interval_seconds > self.max_wait_seconds:
            raise ValueError(
                "poll_interval_seconds is above max_wait_seconds; a poll interval "
                "longer than the longest budget would never get a second read"
            )
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLaneInboxWaitOverlay:
    return load_structured_overlay(NODE_NAME, ModelLaneInboxWaitOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelLaneInboxWaitOverlay",
    "ModelReaderSettings",
    "load_overlay",
]
