# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One supplied snapshot serves ledger cells, the idle alert and SessionStart."""

from pydantic import BaseModel, ConfigDict

from .model_lab_fill_lane_render import ModelLabFillRenderConfig
from .model_lab_fill_plan import ModelLabFillCapacityResult


class ModelLabFillStatusSelection(BaseModel):
    """Grouped skips retain their counts so the row explains zero dispatch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    dispatch: tuple[dict[str, object], ...] = ()
    skipped: tuple[dict[str, object], ...] = ()
    deferred: tuple[dict[str, object], ...] = ()
    budget: int


class ModelLabFillStatusCandidates(BaseModel):
    """Diagnostics distinguish unread queues from queues read empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source: str
    candidates: tuple[dict[str, object], ...] = ()
    detail: str = ""
    not_emitted: dict[str, object] = {}
    filtered: dict[str, object] | None = None
    source_diagnostics: dict[str, dict[str, object] | None] = {}
    open_count_gate: dict[str, object] | None = None


class ModelLabFillStatusRequest(BaseModel):
    """Observed time and apply mode arrive from the caller, never a clock here."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    config: ModelLabFillRenderConfig
    capacity: ModelLabFillCapacityResult
    selection: ModelLabFillStatusSelection
    placements: tuple[dict[str, object], ...] = ()
    candidates: ModelLabFillStatusCandidates | None = None
    dispatched: tuple[dict[str, object], ...] = ()
    observed_at: str
    fire_id: str = ""
    apply: object = True
    probed_approved_depth: object = None
    fallbacks: tuple[dict[str, object], ...] = ()


class ModelLabFillStatusResult(BaseModel):
    """Cells join with ' | '; idle keeps the workflow's snake_case keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cells: tuple[str, ...]
    idle: dict[str, object]
