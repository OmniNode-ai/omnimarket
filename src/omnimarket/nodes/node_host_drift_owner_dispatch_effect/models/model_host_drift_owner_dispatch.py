# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The models of node_host_drift_owner_dispatch_effect (OMN-24239).

The request says only whether the pass places lanes. Everything else comes from the host drift checker's
state file (the open items), the overlay (bounds, actor, dispatch settings) and the ledger (the lanes
already live). The result names every lane the pass placed or would place and every cause it skipped.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from omnimarket.models.model_stall_cause import (
    ModelCauseSkip,
    ModelRowRefusal,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDriftOpenItem(_Frozen):
    """One open item of the host drift checker's state file."""

    host: str
    key: str
    kind: str
    declared: str
    observed: str
    first_seen_at: str
    bound_seconds: int
    verdict: str

    @field_validator("first_seen_at")
    @classmethod
    def _first_seen_is_a_real_stamp(cls, value: str) -> str:
        try:
            datetime.strptime(value, _STAMP)
        except ValueError as exc:
            raise ValueError(
                "first_seen_at must be a UTC stamp of the form YYYY-MM-DDTHH:MM:SSZ"
            ) from exc
        return value


class ModelDriftState(_Frozen):
    """The host the checker ran on and its open items."""

    host: str
    items: tuple[ModelDriftOpenItem, ...]


class ModelDriftOwnerRequest(_Frozen):
    """One pass. A dry run starts no lane and records no dispatch."""

    dry_run: bool


class ModelLiveLanes(_Frozen):
    """The lanes the ledger shows live for the cause keys asked about."""

    covered_causes: tuple[str, ...]
    covered_lanes: tuple[str, ...]


class ModelPlaceOutcome(_Frozen):
    """What the lane runner answered for one placement."""

    started: bool
    detail: str


class ModelDriftLanePlaced(_Frozen):
    """One lane the pass placed (started) or would place (dry run), or failed to place."""

    cause_key: str
    lane: str
    argv: tuple[str, ...]
    brief_path: str
    started: bool
    dry_run: bool
    detail: str


class ModelDriftOwnerResult(_Frozen):
    """The pass: items read, rows rendered, lanes placed, causes skipped, rows refused."""

    host: str
    items_read: int
    rows_rendered: int
    lanes: tuple[ModelDriftLanePlaced, ...]
    skipped: tuple[ModelCauseSkip, ...]
    refusals: tuple[ModelRowRefusal, ...]


__all__ = [
    "ModelDriftLanePlaced",
    "ModelDriftOpenItem",
    "ModelDriftOwnerRequest",
    "ModelDriftOwnerResult",
    "ModelDriftState",
    "ModelLiveLanes",
    "ModelPlaceOutcome",
]
