# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the hourly check's facts read (OMN-23734).

``ModelThroughputTickFacts`` carries the keys the retired throughput_tick.py put on the request it gave
node_throughput_tick_decision_compute. A key the script did not set is not set here either: read the
result with ``model_dump(exclude_unset=True)`` to get the script's request. The exceptions are the
standing-rulings register's judgement and its unread reason (OMN-24478) and, when the register's
journal did not take the read's events, its ``ruling_register_sink_errors`` (OMN-24556), which the
script never read; strip those keys to get the script's request exactly. ``ModelGoalFacts`` carries
what the goal check reads.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_ruling_register import ModelRulingRegisterResult
from omnimarket.models.model_throughput_goal_scope import ModelGoalScope

_TS = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


class ModelProcessOutcome(BaseModel):
    """One program's end."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""


class ModelThroughputTickFactsRequest(BaseModel):
    """Where to read. Paths and program names are the caller's (the launchd agent's), never defaults here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: str = Field(pattern=_TS)
    controller_label: str = "ai.omninode.landing-controller"
    launchctl: str = "launchctl"
    ps: str = "ps"
    landing_state_dir: str
    watcher_state_path: str | None = None
    floors_path: str | None = Field(
        default=None,
        description="The landing floors file; the caller resolves ONEX_LANDING_FLOORS and the bundled default.",
    )
    state_dir: str = Field(
        description="The tick's own state directory (open-count history, repo-status units, goal record)."
    )
    open_basis: str = Field(
        description="The basis the open-count history is kept on (pr_state_local.OPEN_BASIS)."
    )
    repo_status_window_minutes: int = 60
    overlay_roots: str = Field(
        default="",
        description="ONEX_SKILL_OVERLAY_ROOTS as set: colon-separated roots.",
    )
    installed_roots: list[str] = Field(
        default_factory=list,
        description="The installed onex-overlays plugin's roots, searched after overlay_roots.",
    )


class ModelThroughputTickFacts(BaseModel):
    """The facts the decision compute reads, under the script's own key names."""

    model_config = ConfigDict(extra="forbid")

    launchctl: Literal["loaded", "not_loaded", "unavailable"] | None = None
    launchctl_error: str | None = None
    launchctl_stdout: str | None = None
    pid_etime: str | None = None
    ticks: list[dict[str, Any]] | None = None
    ticks_error: str | None = None
    heartbeat_text: str | None = None
    state_json: Any = None
    watcher_path: str | None = None
    watcher_state: Any = None
    watcher_read_error: str | None = None
    floors_per_repo: dict[str, float] | None = None
    floors_error: str | None = None
    policy_load_error: str | None = None
    land_like_operator: list[str] | None = None
    open_history: list[dict[str, Any]] | None = None
    repo_status: dict[str, Any] | None = None
    ruling_register: ModelRulingRegisterResult | None = Field(
        default=None,
        description=(
            "The standing-rulings register's judgement (OMN-24478), read from the "
            "node_ruling_register_compute overlay; the tick decision prints one MISSING "
            "ruling-not-running line per row that is not TRUE."
        ),
    )
    ruling_register_unread: str | None = Field(
        default=None,
        description=(
            "Why the standing-rulings register could not be read, when it could not; "
            "never a raise."
        ),
    )
    ruling_register_sink_errors: list[str] | None = Field(
        default=None,
        description=(
            "The register read's journal write failures (OMN-24556), one "
            "``<row_id>: <exception name>`` entry per row whose read event the "
            "journal did not take; unset when the journal took them all, so a facts "
            "result without failures reads exactly as before."
        ),
    )


class ModelGoalFacts(BaseModel):
    """What the goal check reads: the scope (or why not), the last record, the controller's admissions and receipt reasons."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ModelGoalScope | None = None
    scope_unread: str = ""
    previous: dict[str, Any] | None = None
    admissions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    admissions_unread: str = ""
    pr_reasons: dict[str, str] | None = None
    pr_reasons_unread: str = ""


class ModelThroughputTickFactsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: ModelThroughputTickFacts
    goal: ModelGoalFacts


__all__ = [
    "ModelGoalFacts",
    "ModelProcessOutcome",
    "ModelThroughputTickFacts",
    "ModelThroughputTickFactsRequest",
    "ModelThroughputTickFactsResult",
]
