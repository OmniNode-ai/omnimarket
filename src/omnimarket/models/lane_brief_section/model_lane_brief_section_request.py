# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request for the lane-brief section 3a render (OMN-21118, plan task TA.17)."""

from __future__ import annotations

from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from pydantic import BaseModel, ConfigDict, Field

#: LANE_RULES entry slots, by `source_ref`; entries sharing a slot render in the order given.
SLOT_LANDING_RULES = "landing-rules"
SLOT_PREAMBLE = "preamble"
SLOT_CLAIM_TEMPLATE = "claim-template"
SLOT_DELEGATION = "delegation"
SLOT_LAB = "lab"
#: OPERATOR_RULINGS entry slots.
SLOT_LANDING_RULING = "landing-ruling"
SLOT_STANDING_RULINGS = "standing-rulings"
#: OPEN_WORK entry slots.
SLOT_INBOX = "inbox"
SLOT_HOLDS = "holds"


class ModelLaneBriefSectionRequest(BaseModel):
    """The three required factors of the `lane_brief_section` profile, as sources returned them.

    Each factor is a `ModelContextSourceResult`; its entries are placed by `source_ref`
    slot. Header values (the reader return codes, the pinned T0, the lane and model) are
    typed fields, never parsed out of entry text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    ledger_t0: str = Field(
        min_length=1, description="CATCHUP_T0 the holds reader was pinned to"
    )
    hours: int = Field(gt=0)
    rulings_tail: int = Field(gt=0)
    inbox_rc: int
    holds_rc: int
    landing_lane: str = Field(min_length=1)
    landing_ruling: str = Field(
        min_length=1, description="UTC stamp of the cited landing RULING row"
    )
    landing_ruling_lane: str = Field(min_length=1)
    cited_ruling_stamps: tuple[str, ...] = Field(
        default=(),
        description="Ruling row stamps the brief cites; each must be backed by an OPERATOR_RULINGS entry",
    )
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    route_line: str | None = None
    consent_lines: tuple[str, ...] = ()
    out_of_scope: str = Field(min_length=1)
    lane_rules: ModelContextSourceResult | None = None
    operator_rulings: ModelContextSourceResult | None = None
    open_work: ModelContextSourceResult | None = None
