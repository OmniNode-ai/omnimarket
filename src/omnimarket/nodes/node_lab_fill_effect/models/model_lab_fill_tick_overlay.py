# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill unit's deployment overlay: every setting, no code default for a deployment fact (OMN-24419).

The Mac lab-fill unit reads it, finds the pool snapshot, runs the select step and builds the tick request from it.
Paths may start with $OMNIBASE_PATH or $OMNI_HOME; the runtime expands them, this model never does."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .model_lab_fill_effect import LANE_PATTERN
from .model_lab_fill_tick import (
    ModelLabFillTickCloud,
    ModelLabFillTickHost,
    ModelLabFillTickRequest,
)

OVERLAY_ID = "node_lab_fill_effect"
# A second instance of the step pins its own overlay file here;
# unset, the overlay is <root>/node_lab_fill_effect/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS.
OVERLAY_FILE_ENV = "ONEX_LAB_FILL_TICK_OVERLAY_FILE"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ModelLabFillTickOverlay(BaseModel):
    """The lab-fill tick step's deployment overlay (OMN-24419)."""

    model_config = _FROZEN

    parent: str = Field(pattern=LANE_PATTERN)
    parent_ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    authority_ruling: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$")
    authority_lane: str = Field(pattern=LANE_PATTERN)
    landing_lane: str = Field(pattern=LANE_PATTERN)
    engine: Literal["sonnet", "codex"]
    timeout_min: int = Field(ge=1, le=180)
    max_lanes: int = Field(ge=1, le=12)
    max_per_host: int = Field(ge=1, le=12)
    mvp_repos: tuple[str, ...]
    process_repos: tuple[str, ...]
    alarm_after: int = Field(ge=1)
    diagnose_cooldown_min: int = Field(ge=1)
    park_after: int = Field(ge=1)
    park_hours: int = Field(ge=1)
    cloud: ModelLabFillTickCloud
    host_reach: Mapping[str, tuple[str, ...]]
    fact_hosts: tuple[str, ...]
    ledger_path: str
    approved_work_path: str
    brief_dir: str
    state_path: str
    pool_snapshot_path: str
    pool_record_suffix: str
    pool_snapshot_max_age_seconds: int = Field(ge=1)
    pool_engine: str
    selection_ledger_reader: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    selection_ledger_window_hours: int = Field(ge=1, le=720)
    selection_attempt_limit: int = Field(ge=1)
    selection_attempt_window_hours: int = Field(ge=1)
    selection_cooldown_hours: int = Field(ge=1)
    selection_repeat_limit: int = Field(ge=1)
    selection_scope_repos: tuple[str, ...]

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

    def tick_request(
        self,
        *,
        run_key: str,
        fire_id: str,
        hosts: tuple[ModelLabFillTickHost, ...],
        selection_holds: Mapping[str, str],
        ledger_path: str,
        approved_work_path: str,
        brief_dir: str,
        state_path: str,
    ) -> ModelLabFillTickRequest:
        """Build the tick request from the overlay fields plus the fire arguments."""
        return ModelLabFillTickRequest(
            run_key=run_key,
            fire_id=fire_id,
            apply=True,
            hosts=hosts,
            ledger_path=ledger_path,
            approved_work_path=approved_work_path,
            brief_dir=brief_dir,
            parent=self.parent,
            parent_ticket=self.parent_ticket,
            authority_ruling=self.authority_ruling,
            authority_lane=self.authority_lane,
            landing_lane=self.landing_lane,
            engine=self.engine,
            timeout_min=self.timeout_min,
            max_lanes=self.max_lanes,
            max_per_host=self.max_per_host,
            mvp_repos=self.mvp_repos,
            process_repos=self.process_repos,
            state_path=state_path,
            alarm_after=self.alarm_after,
            diagnose_cooldown_min=self.diagnose_cooldown_min,
            park_after=self.park_after,
            park_hours=self.park_hours,
            cloud=self.cloud,
            host_reach=self.host_reach,
            fact_hosts=self.fact_hosts,
            selection_holds=selection_holds,
        )
