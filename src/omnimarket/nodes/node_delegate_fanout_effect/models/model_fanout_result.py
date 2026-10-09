# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every item yields a row, including requests that never reached the bus."""

from pydantic import BaseModel, ConfigDict


class ModelFanoutRow(BaseModel):
    """Receipt facts, never an interpretation of the subprocess exit status."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str
    lane: str
    correlation_id: str | None = None
    run_id: str | None = None
    terminal: str = "NO_RECEIPT"
    terminal_failure_cause: str | None = None
    quality: float | None = None
    model: str | None = None
    cost_usd: float | None = None
    wall_ms: float | None = None
    artifacts: str | None = None
    result_excerpt: str = ""
    last_stdout_line: str | None = None


class ModelFanoutResult(BaseModel):
    """The table and the exact ledger closeout fragment for the whole batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    lane: str
    items: int
    admitted: int
    refused: int
    rows: list[ModelFanoutRow]
    receipts: int
    failed: int
    correlation_ids: list[str]
    closeout_fragment: str
    markdown_table: str
