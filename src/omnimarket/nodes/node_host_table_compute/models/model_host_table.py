# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed row set of the lab host table: every host, the admitted names, the excluded rows."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.overlay_keys import ModelLabHostsTable


class ModelHostTableRequest(BaseModel):
    """The lab host table to turn into a row set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: ModelLabHostsTable


class ModelHostTableRow(BaseModel):
    """One host of the table: admitted, or excluded with its stated reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    cap: int | None
    available: bool
    # Empty when the host is available.
    exclusion_reason: str


class ModelHostTableResult(BaseModel):
    """The row set: every row, the admitted names, the excluded rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Every row of the table, in table order.
    rows: tuple[ModelHostTableRow, ...]
    # The names of the available rows, in table order.
    admitted: tuple[str, ...]
    # The unavailable rows, in table order.
    excluded: tuple[ModelHostTableRow, ...]
