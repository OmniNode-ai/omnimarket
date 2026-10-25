# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-drift open items in, the running-vs-merged findings out (OMN-19998)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelRunningSurface(BaseModel):
    """One open item of the host-drift fleet fold: what one host runs against what it declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    # The fold's item key, such as plugin:omni@omninode-internal or dispatch-venv:<path>.
    key: str
    kind: str
    observed: str = ""
    first_seen_at: AwareDatetime
    verdict: str = "DRIFT"
    # A recorded branch install on purpose (install.sh --branch-hours writes branch_until and
    # branch_source into landing_install.json); None when the item carries no readable hold.
    branch_until: AwareDatetime | None = None
    branch_source: str = ""


class ModelRunningVsMergedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    # False when the fleet fold could not be read: the verdict is then UNKNOWN, never OK.
    fleet_readable: bool = True
    unreadable_why: str = ""
    # When the fold was written; a fold older than ``fleet_max_age_minutes`` cannot vouch for now.
    folded_at: AwareDatetime | None = None
    items: tuple[ModelRunningSurface, ...] = ()
    bound_minutes: int = Field(default=60, gt=0)
    fleet_max_age_minutes: int = Field(default=45, gt=0)


class ModelRunningVsMergedFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    level: Literal["missing", "unknown", "note"]
    # The tick's loop name: running-vs-merged:<surface>.
    loop: str
    why: str
    fix: str


class ModelRunningVsMergedResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["OK", "MISSING", "UNKNOWN"]
    findings: tuple[ModelRunningVsMergedFinding, ...] = ()
    # Note-level findings: differences that are expected, such as a live recorded branch hold.
    acknowledged: tuple[ModelRunningVsMergedFinding, ...] = ()
