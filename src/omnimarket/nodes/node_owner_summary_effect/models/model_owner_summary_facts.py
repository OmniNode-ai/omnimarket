# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the factory did in one window, as typed facts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_owner_summary_effect.models.model_clone_commit import (
    ModelCloneCommit,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_draft_coverage import (
    ModelDraftCoverage,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_closed import (
    ModelLaneClosed,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_lane_started import (
    ModelLaneStarted,
)
from omnimarket.nodes.node_owner_summary_effect.models.model_m4_row import ModelM4Row
from omnimarket.nodes.node_owner_summary_effect.models.model_merged_change import (
    ModelMergedChange,
)


class ModelOwnerSummaryFacts(BaseModel):
    """Merges, the open queue, lanes, M4 rows and friction data for a window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: str
    window_since: str
    window_until: str
    window_source: str
    full_list_link: str = ""
    merged_changes: tuple[ModelMergedChange, ...] = ()
    clone_only_commits: tuple[ModelCloneCommit, ...] = ()
    merged_by_repo: dict[str, int] = {}
    clone_commits_by_repo: dict[str, int] = {}
    watcher_state_age_s: int | None = None
    clone_fetch_age_max_s: int = 0
    open_total: int = 0
    open_drafts: int = 0
    open_by_repo: dict[str, int] = {}
    open_by_class: dict[str, int] = {}
    lanes_closed_count: int = 0
    lanes_closed: tuple[ModelLaneClosed, ...] = ()
    lanes_closed_omitted: int = 0
    lanes_closed_by_outcome: dict[str, int] = {}
    controller_worker_lanes_closed: int = 0
    lanes_started_not_closed: tuple[ModelLaneStarted, ...] = ()
    m4_rows: tuple[ModelM4Row, ...] = ()
    friction_status: Literal["ok", "unavailable"] = "unavailable"
    friction_reason: str = ""
    friction_top_dropped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    coverage: ModelDraftCoverage = ModelDraftCoverage()
    open_by_class_plain: dict[str, str] = {}
    plain_words: dict[str, str] = {}
    draft_rules: tuple[str, ...] = ()
