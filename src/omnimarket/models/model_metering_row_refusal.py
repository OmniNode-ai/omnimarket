# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why ``onex metering`` prints no figures for a window (OMN-19977).

The command renders the stored ``metering-summary.v1`` row and never computes
one. When that row is absent, or older than the evidence it summarises, it
answers with this typed state instead, naming the repair.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

METERING_ROW_REPAIR = (
    "Run `onex delegate` once: the end of every delegation refreshes this "
    "install's metering rows."
)


class EnumMeteringRowState(StrEnum):
    """The two reasons a stored row is not printed."""

    #: No row for this tenant, window and baseline.
    MISSING = "METERING_SUMMARY_MISSING"
    #: A row exists, but a recorded run is newer than it, or it was priced
    #: against another pricing manifest version than the current one.
    STALE = "METERING_SUMMARY_STALE"


class ModelMeteringRowRefusal(BaseModel):
    """A typed answer in place of a row that is absent or older than its evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: EnumMeteringRowState
    reason: str
    repair: str
    tenant_id: str
    window_kind: Literal["day", "all"]
    window_start: str
    baseline_model: str
    row_as_of: str | None = None
    newest_run_at: str | None = None
    row_pricing_manifest_version: str | None = None
    current_pricing_manifest_version: str | None = None


__all__ = [
    "METERING_ROW_REPAIR",
    "EnumMeteringRowState",
    "ModelMeteringRowRefusal",
]
