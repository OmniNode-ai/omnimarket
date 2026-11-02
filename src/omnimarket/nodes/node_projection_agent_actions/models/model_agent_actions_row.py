# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The row one delivery writes, and the table it goes to."""

from pydantic import BaseModel, ConfigDict, Field


class ModelAgentActionsRow(BaseModel):
    """One row of one agent_* table, keyed on the table's conflict column."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str = Field(min_length=1)
    conflict_column: str = Field(min_length=1)
    values: dict[str, object] = Field(
        description="Column to value, in the table's insert order."
    )
