# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``lab_pass_receipts`` overlay: where the lane-health projection is served, and which lane's receipt counts (OMN-24542).

The projection's address and the lane whose lab-pass receipt counts as deployed evidence are deployment facts,
so they are overlay values and not code: the overlay ``lab_pass_receipts`` names both, and no environment
variable carries either.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabPassReceiptsOverlay(BaseModel):
    """The lane-health projection address the dev lane's lab-pass receipt is read from and the lane whose receipt counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipts_url: str = Field(pattern=r"^https?://[^\s]+$")
    lane: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
