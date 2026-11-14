# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill select step's request and result (OMN-19998).

The request carries the selection facts as a JSON-shaped dict (a ModelLabFillSelectionRequest
WITHOUT its ledger rows) and the bus reader's name and window; the result carries the ledger read's
status and, only when it is ok, the selection's decisions.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.lab_fill.model_lab_fill_selection_result import (
    ModelLabFillSelectionResult,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")

__all__ = ["ModelLabFillSelectRequest", "ModelLabFillSelectResult"]


class ModelLabFillSelectRequest(BaseModel):
    """One select step: the selection facts (without ledger rows) and the bus reader to read them from."""

    model_config = _FROZEN

    selection: dict[str, Any]
    ledger_reader: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    ledger_window_hours: int = Field(ge=1, le=720)

    @field_validator("selection")
    @classmethod
    def _no_ledger_lines(cls, value: dict[str, Any]) -> dict[str, Any]:
        if "ledger_lines" in value:
            raise ValueError(
                "selection must not carry ledger_lines: the ledger arrives only from the bus reader"
            )
        return value


class ModelLabFillSelectResult(BaseModel):
    """The select step's answer: the ledger read's status and, when ok, the selection's result."""

    model_config = _FROZEN

    ledger_status: Literal["ok", "stale", "unreadable", "not-moved"]
    ledger_reason: str
    ledger_rows: int
    ledger_watermark_age_s: int | None = None
    selection: ModelLabFillSelectionResult | None = None

    def holds(self) -> dict[str, str]:
        """ticket -> the selection's skip reason for every decision the selection held.

        Empty when the ledger read was not ok (nothing was selected): a driver reads ``ledger_status`` before
        it treats an empty answer as "nothing held".
        """
        if self.selection is None:
            return {}
        return {
            decision.ticket: decision.reason.value
            for decision in self.selection.decisions
            if decision.reason is not None
        }
