# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request and result of node_commitment_ledger_compute."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_commitment_ledger_compute.models.model_commitment import (
    ModelCommitment,
)
from omnimarket.nodes.node_commitment_ledger_compute.models.model_commitment_rules import (
    ModelCommitmentRules,
)


class ModelCommitmentLedgerRequest(BaseModel):
    """`build_open_row` writes the row that records an ask; `fold` reads every commitment from ledger rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["build_open_row", "fold"]
    rules: ModelCommitmentRules
    now: datetime
    # build_open_row: the lane writing the row, the ask as the operator said it, who owes it and the check.
    lane: str = ""
    ask: str = ""
    owner: str = ""
    check: str = ""
    ticket: str | None = None
    requires: tuple[str, ...] = ()
    # fold: the ledger rows to read; rows of other kinds are skipped.
    ledger_rows: tuple[ModelLedgerEvidenceRow, ...] = ()


class ModelCommitmentLedgerResult(BaseModel):
    """The row built, or why none was; the folded commitments and the rows that did not read as one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["build_open_row", "fold"]
    row: str | None = None
    refusals: tuple[str, ...] = ()
    commitments: tuple[ModelCommitment, ...] = ()
    # A commitment-kind row that could not be read (a missing cell, an orphan result), named and never dropped.
    malformed: tuple[str, ...] = ()
