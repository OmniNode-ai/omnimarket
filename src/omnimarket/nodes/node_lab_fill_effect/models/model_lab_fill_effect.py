# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the lab-fill effect operations (OMN-20668).

The effect decides nothing about which work runs where: the plan compute node
(node_lab_fill_plan_compute) does. These models carry what the effect reads and
what it did, with the field names the old workflow's agents returned, so the
compute node's inputs are these results unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lab_fill import ModelLabFillLaunch

_FROZEN = ConfigDict(frozen=True, extra="forbid")
RUN_KEY_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4}Z)?$"
LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class ModelLabFillProbeRequest(BaseModel):
    """Read every lab host's headroom through the runner's own pool read."""

    model_config = _FROZEN

    run_key: str = Field(pattern=RUN_KEY_PATTERN)
    ledger_path: str
    approved_work_path: str = ""
    force: bool = False


class ModelLabFillProbeResult(BaseModel):
    """The pool readings, or why the run is already delivered or the pool is unreadable."""

    model_config = _FROZEN

    outcome: Literal["read", "already-delivered", "unreadable"]
    readings: tuple[Mapping[str, object], ...] = ()
    approved_work_depth: int | None = None
    precheck_evidence: str = ""
    clock_utc: str
    finished_utc: str
    notes: str = ""


class ModelLabFillOwnerLane(BaseModel):
    """A selected lane whose owner is to be read."""

    model_config = _FROZEN

    lane: str = Field(pattern=LANE_PATTERN)
    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    pr: str = ""
    repo: str = ""
    kind: str = ""


class ModelLabFillOwnerFactsRequest(BaseModel):
    """Read every ownership source once for the selected lanes."""

    model_config = _FROZEN

    lanes: tuple[ModelLabFillOwnerLane, ...]
    ledger_path: str
    pr_claim_cli: str = ""
    watcher_state_path: str = ""


class ModelLabFillOwnerFacts(BaseModel):
    """What each source said, or the text of why it could not say.

    The field names and shapes are the plan node's ownership request, so the two
    nodes meet without translation. A source that failed is a string, never an empty
    answer: an unread owner is not a free one.
    """

    model_config = _FROZEN

    claim_index: dict[str, dict[str, str]] | str
    ledger_claims: tuple[dict[str, str], ...] | str
    now: str
    staleness_hours: float
    pr_claims: dict[str, str] | str
    watcher_merged: dict[str, tuple[str, ...]] | str


class ModelLabFillLaunchRequest(BaseModel):
    """Launch one lane inside its share of the dispatch window."""

    model_config = _FROZEN

    launch: ModelLabFillLaunch
    kind: str
    brief: str = Field(min_length=1)
    brief_dir: str
    ledger_path: str
    approved_work_path: str = ""
    operator_id: str = ""
    window_end_epoch_s: int | None = None
    lanes_left: int = Field(default=1, ge=1)
    min_lane_s: int = Field(default=15, ge=1)
    default_slice_s: int = Field(default=420, ge=1)


class ModelLabFillLaunchResult(BaseModel):
    """What happened to one lane: detached with a receipt, or skipped with the reason."""

    model_config = _FROZEN

    lane: str
    detached: bool
    receipt: str = ""
    skipped: str = ""
    brief_has_delegation: bool = False
    detail: str = ""
    elapsed_s: int = 0


class ModelLabFillReceiptLane(BaseModel):
    """One detached lane whose runner receipt is to be read."""

    model_config = _FROZEN

    lane: str
    receipt: str
    engine: str = "unknown"


class ModelLabFillReceiptsRequest(BaseModel):
    """Re-read receipts until each names a host, the window ends or the deadline passes."""

    model_config = _FROZEN

    lanes: tuple[ModelLabFillReceiptLane, ...]
    window_s: int = Field(default=150, ge=0, le=3600)
    end_epoch_s: int | None = None
    codex_wait_s: int = Field(default=45, ge=0)
    poll_s: int = Field(default=15, ge=1)


class ModelLabFillReceiptsResult(BaseModel):
    """Each receipt as last read; a field the receipt does not carry is absent."""

    model_config = _FROZEN

    receipts: tuple[Mapping[str, object], ...]


class ModelLabFillFallbackHostRequest(BaseModel):
    """Ranked hosts for a pinned fallback, read against the runner's limited markers now."""

    model_config = _FROZEN

    ranked_hosts: tuple[str, ...]


class ModelLabFillFallbackHostResult(BaseModel):
    """The first ranked host whose engine login is not limited; empty when all are."""

    model_config = _FROZEN

    host: str = ""


class ModelLabFillStatusWriteRequest(BaseModel):
    """The cells the plan node composed for the run's one STATUS row, and its idle result."""

    model_config = _FROZEN

    run_key: str = Field(pattern=RUN_KEY_PATTERN)
    cells: tuple[str, ...] = Field(min_length=1)
    friction_cells: str = ""
    idle: Mapping[str, object]
    result_path: str = ""
    requested_by_lane: str = Field(default="lab-fill", pattern=LANE_PATTERN)
    ledger_id: str = "rolling-work-ledger"


class ModelLabFillStatusWriteResult(BaseModel):
    """Whether the STATUS row (and the friction row, when the idle alarm fired) was written."""

    model_config = _FROZEN

    outcome: Literal["appended", "duplicate", "friction-refused", "refused", "error"]
    row: str = ""
    friction_recorded: bool = False
    result_written: bool = False
    ledger_lines: tuple[int, ...] = ()
    message: str = ""
