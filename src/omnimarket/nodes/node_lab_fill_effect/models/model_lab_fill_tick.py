# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of one lab-fill tick (OMN-17427).

A tick is the whole fill with no model in the loop: the headroom slots it is given,
the approved-work list, the ownership read, the launch of each lane through the
remote-lane runner. The result is the tick's event: what it dispatched, and for each
row that did not go, why.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_lab_fill_effect import LANE_PATTERN, RUN_KEY_PATTERN

_FROZEN = ConfigDict(frozen=True, extra="forbid")
PriorityClass = Literal["mvp-landing", "process-fix", "other"]


class ModelLabFillTickHost(BaseModel):
    """One pool host and the lanes it can take now, from the headroom plan."""

    model_config = _FROZEN

    host: str = Field(min_length=1)
    free: int = Field(ge=0)


class ModelLabFillTickRequest(BaseModel):
    """Fill the free slots of one fire from the approved-work list."""

    model_config = _FROZEN

    run_key: str = Field(pattern=RUN_KEY_PATTERN)
    fire_id: str = ""
    apply: bool = True
    hosts: tuple[ModelLabFillTickHost, ...]
    ledger_path: str
    approved_work_path: str
    brief_dir: str
    operator_id: str = ""
    parent: str = Field(pattern=LANE_PATTERN)
    parent_ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    authority_ruling: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$")
    authority_lane: str = Field(pattern=LANE_PATTERN)
    landing_lane: str = Field(default="landing-controller", pattern=LANE_PATTERN)
    engine: Literal["sonnet", "codex"] = "sonnet"
    timeout_min: int = Field(default=60, ge=1, le=180)
    max_lanes: int = Field(default=12, ge=1, le=12)
    max_per_host: int = Field(default=12, ge=1, le=12)
    mvp_repos: tuple[str, ...] = ()
    process_repos: tuple[str, ...] = ()
    window_end_epoch_s: int | None = None
    # The tick's memory between fires: the idle streak, the diagnose lane's last dispatch
    # and the parked candidates. Empty keeps no memory, so there is no alarm and no parking.
    state_path: str = ""
    # Ticks with free slots and nothing placed before the diagnose lane goes out.
    alarm_after: int = Field(default=2, ge=1)
    # Minutes after a diagnose dispatch in which another is deduplicated.
    diagnose_cooldown_min: int = Field(default=180, ge=1)
    # Brief-check refusals of one ticket before it is parked, and for how many hours.
    park_after: int = Field(default=2, ge=1)
    park_hours: int = Field(default=24, ge=1)

    @field_validator("ledger_path", "approved_work_path", "brief_dir")
    @classmethod
    def _path_stays_put(cls, value: str) -> str:
        """A path is named, never walked up out of its directory, and holds no NUL."""
        if not value or "\x00" in value or ".." in value.replace("\\", "/").split("/"):
            raise ValueError("path must be non-empty, hold no NUL and no '..' segment")
        return value

    @field_validator("state_path")
    @classmethod
    def _state_path_stays_put(cls, value: str) -> str:
        """The memory path is empty (no memory) or a named path with no NUL and no '..' segment."""
        if value and ("\x00" in value or ".." in value.replace("\\", "/").split("/")):
            raise ValueError("state_path must hold no NUL and no '..' segment")
        return value


class ModelLabFillTickDispatch(BaseModel):
    """A lane the tick launched, or would launch when ``apply`` is false."""

    model_config = _FROZEN

    lane: str
    ticket: str
    row_id: str
    host: str
    priority: PriorityClass
    receipt: str = ""


class ModelLabFillTickSkip(BaseModel):
    """A row the tick tried or considered and did not dispatch, with the reason."""

    model_config = _FROZEN

    ticket: str
    row_id: str
    reason: str
    detail: str = ""


class ModelLabFillTickRefusal(BaseModel):
    """A lane the tick made for a candidate or would have made, and did not place, with the reason."""

    model_config = _FROZEN

    lane: str
    ticket: str
    row_id: str
    host: str
    reason: str
    detail: str = ""


class ModelLabFillTickAlarm(BaseModel):
    """Free slots went unplaced: how many ticks running, and what the tick did about it."""

    model_config = _FROZEN

    streak: int
    free_slots: int
    diagnose: Literal["none", "dispatched", "deduplicated", "refused"] = "none"
    diagnose_lane: str = ""
    diagnose_receipt: str = ""
    diagnose_detail: str = ""


def render_refusals(refusals: tuple[ModelLabFillTickRefusal, ...]) -> str:
    """``lane@host:reason`` per refusal, comma-joined, ledger separators made harmless; ``none`` when empty."""
    return (
        ",".join(
            re.sub(r"[|\n\r]", "/", f"{r.lane}@{r.host}:{r.reason}").strip()
            for r in refusals
        )
        or "none"
    )


class ModelLabFillTickResult(BaseModel):
    """The tick's event: what went where, what did not and why, what was never tried."""

    model_config = _FROZEN

    run_key: str
    fire_id: str = ""
    outcome: Literal[
        "dispatched",
        "planned",
        "nothing-dispatched",
        "no-headroom",
        "approved-work-unreadable",
    ]
    free_slots: int
    dispatched: tuple[ModelLabFillTickDispatch, ...] = ()
    skipped: tuple[ModelLabFillTickSkip, ...] = ()
    untried: tuple[str, ...] = ()
    stopped: str = ""
    detail: str = ""
    # Every lane made and not placed, with its reason; ``untried:<why>`` for the rest of a stopped tick.
    refusals: tuple[ModelLabFillTickRefusal, ...] = ()
    # Tickets parked by this tick after their second brief-check refusal.
    parked: tuple[str, ...] = ()
    alarm: ModelLabFillTickAlarm | None = None
    # Lanes of approved work the runner detached; a plan-only tick places none.
    placed: int = 0
    # The STATUS row's ``refusals=`` value: ``none`` only when every lane made was placed.
    refusals_cell: str = "none"


__all__: list[str] = [
    "ModelLabFillTickAlarm",
    "ModelLabFillTickDispatch",
    "ModelLabFillTickHost",
    "ModelLabFillTickRefusal",
    "ModelLabFillTickRequest",
    "ModelLabFillTickResult",
    "ModelLabFillTickSkip",
    "PriorityClass",
    "render_refusals",
]
