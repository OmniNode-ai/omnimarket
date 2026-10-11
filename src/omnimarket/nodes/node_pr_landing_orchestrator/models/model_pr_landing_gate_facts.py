# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ledger facts the arm decision reads, and its answer (OMN-20866).

The facts come from the lab projections the bus feeds: the open HOLD entities
of ``work_ledger_state`` (folded from the ledger's row events by
node_projection_work_ledger), the pr-head lab proof receipts of
``lab_proof_receipts`` (node_projection_lab_proof_receipts) and the lab pool's
``LAB PROOF PASS`` readback rows of ``work_ledger_rows``. Each fact carries a
state: UNKNOWN means it could not be read, and the decision fails closed on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_fact_state import (
    EnumPrLandingFactState,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.enum_pr_landing_withheld_reason import (
    EnumPrLandingWithheldReason,
)


class ModelPrLandingLedgerHold(BaseModel):
    """One open HOLD entity of the ledger projection, as its opening row states it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hold_id: str = Field(..., min_length=1, description="The row's id= cell.")
    repo: str | None = Field(default=None, description="The repo= cell.")
    pr: str | None = Field(default=None, description="The pr= cell, as written.")
    surface: str | None = Field(default=None, description="The surface= cell.")
    until_at: datetime | None = Field(default=None, description="The until= cell.")
    raw_row: str = Field(
        default="", description="The opening row's first line ('' when unread)."
    )


class ModelPrLandingLabPass(BaseModel):
    """One lab proof result that names the PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["lab_proof_receipt", "ledger_readback"]
    ref: str = Field(..., min_length=1, description="Receipt key or ledger row id.")
    head_sha: str = Field(
        ...,
        pattern=r"^[0-9a-f]{10,40}$",
        description="The proven head: full on a receipt, ten or more on a readback.",
    )
    result: str = Field(..., min_length=1)
    verifier_token: str | None = Field(
        default=None,
        description="The verifier's token on a receipt; None on a readback.",
    )


class ModelPrLandingGateFacts(BaseModel):
    """What the projections said about one PR head when the arm was decided."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    pr_number: int = Field(..., ge=1)
    head_sha: str
    read_at: datetime
    holds_state: EnumPrLandingFactState
    holds: tuple[ModelPrLandingLedgerHold, ...] = ()
    ledger_newest_projected_at: datetime | None = Field(
        default=None,
        description="The ledger projection's newest projected_at (its freshness).",
    )
    lab_state: EnumPrLandingFactState
    lab_passes: tuple[ModelPrLandingLabPass, ...] = ()
    unknown_detail: str | None = Field(
        default=None, description="Why a state is UNKNOWN, redacted."
    )


class ModelPrLandingGateDecision(BaseModel):
    """ARM-eligible (``withheld`` None) or the one named reason it is not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    withheld: EnumPrLandingWithheldReason | None = None
    detail: str = ""
    hold_ids: tuple[str, ...] = ()
    lab_pass_ref: str | None = None

    @property
    def reason_text(self) -> str | None:
        """``<code>: <detail>``, the text the transition carries; None when not withheld."""
        if self.withheld is None:
            return None
        return f"{self.withheld.value}: {self.detail}"


__all__: list[str] = [
    "EnumPrLandingFactState",
    "EnumPrLandingWithheldReason",
    "ModelPrLandingGateDecision",
    "ModelPrLandingGateFacts",
    "ModelPrLandingLabPass",
    "ModelPrLandingLedgerHold",
]
