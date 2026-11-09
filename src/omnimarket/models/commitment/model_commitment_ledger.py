# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of node_commitment_ledger_compute."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.commitment.model_commitment import (
    ModelCommitment,
)
from omnimarket.models.commitment.model_commitment_rules import (
    ModelCommitmentRules,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow


class ModelCommitmentLedgerRequest(BaseModel):
    """`build_open_row` writes the row that records an ask, `build_withdraw_row` the row that withdraws it; `fold` reads
    every commitment from ledger rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["build_open_row", "build_withdraw_row", "fold"]
    rules: ModelCommitmentRules
    now: datetime
    # build_open_row: the lane writing the row, the ask as the operator said it, who owes it and the check.
    lane: str = ""
    ask: str = ""
    owner: str = ""
    check: str = ""
    ticket: str | None = None
    requires: tuple[str, ...] = ()
    # build_open_row: a short token the writer draws per ask; it keeps two asks of one lane in one second distinct.
    nonce: str = ""
    # build_withdraw_row: the commitment no longer wanted (lane is the writer).
    commitment_id: str = ""
    # fold: the ledger rows to read; rows of other kinds are skipped.
    ledger_rows: tuple[ModelLedgerEvidenceRow, ...] = ()


class ModelCommitmentLedgerResult(BaseModel):
    """The row built, or why none was; the folded commitments and the rows that did not read as one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["build_open_row", "build_withdraw_row", "fold"]
    row: str | None = None
    refusals: tuple[str, ...] = ()
    commitments: tuple[ModelCommitment, ...] = ()
    # A commitment-kind row that could not be read (a missing cell, an orphan result), named and never dropped.
    malformed: tuple[str, ...] = ()
