# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readings feeder's rules: which ledger rows are reading rows and which cells carry the fact (OMN-23427)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelReadingsFeedRules(BaseModel):
    """How the readings feeder folds the ledger's reading rows into the readings file.

    ``row_type`` and ``term`` select the rows (the cells node_reading_writer_effect writes on every
    reading row), ``key_cell`` names the cell carrying the reading's key, ``value_cell`` the cell
    carrying its value, ``published_cell`` the cell whose ISO time decides which row of a key is the
    newest, and ``lookback_hours`` how far back one feed reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_type: str = Field(description="the ledger row type that carries reading rows.")
    term: str = Field(
        description="only rows whose line carries this term are reading rows."
    )
    key_cell: str = Field(description="the cell naming the reading's key.")
    value_cell: str = Field(description="the cell carrying the reading's value.")
    published_cell: str = Field(
        description="the cell whose ISO time orders the rows of one key; the ledger stamp backs it up."
    )
    lookback_hours: float = Field(
        gt=0, description="how far back one feed reads the ledger."
    )


__all__ = ["ModelReadingsFeedRules"]
