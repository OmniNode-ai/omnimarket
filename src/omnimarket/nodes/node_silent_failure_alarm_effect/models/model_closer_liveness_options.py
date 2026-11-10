# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the ticket closer declares its liveness and leaves its run records (OMN-20718)."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelCloserLivenessOptions(BaseModel):
    """The closer's staleness check reads its interval from the liveness overlay entry.

    ``liveness_overlay`` is the file the automation-liveness checker reads and
    ``process_id`` the closer's entry in it, so the interval is declared once. A run record is
    a TERMINAL row of ``lane`` in the ledger or a receipt of ``lane`` in ``fire_receipts``,
    whichever is newer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = "dod-closeout-sweep"
    unit_label: str = "ai.omninode.dod-closeout-sweep"
    liveness_overlay: Path
    process_id: str = "h200/dod-closeout-sweep"
    stale_multiple: int = Field(default=2, ge=1)
    fire_receipts: Path | None = None
    # Receipt phases that mean the fire ran, whatever it flipped.
    run_phases: tuple[str, ...] = ("completed", "degraded", "result_invalid", "failed")
