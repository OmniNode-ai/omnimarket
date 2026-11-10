# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The commitment rows' rules: overlay data, never constants in the handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCommitmentRules(BaseModel):
    """The overlay of node_commitment_ledger_compute. An absent key refuses the load and names it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The canonical ledger row type a commitment rides on, the cell that marks the row's kind and that kind's value.
    row_type: str = Field(min_length=1)
    kind_cell: str = Field(min_length=1)
    kind_value: str = Field(min_length=1)
    # A longer ask or check is refused, so a row stays one readable line.
    max_ask_chars: int = Field(ge=1)
    max_check_chars: int = Field(ge=1)
