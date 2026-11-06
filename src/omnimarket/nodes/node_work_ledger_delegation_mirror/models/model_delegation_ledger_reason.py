# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The evidence behind a delegation terminal's ledger reason cell."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelDelegationLedgerReason(BaseModel):
    """A reason and its provenance; missing evidence stays explicit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    source: Literal[
        "terminal_failure_cause",
        "terminal_failure_reason",
        "failure_evidence",
        "quality_gates_failed",
        "status",
        "unreported",
    ]
