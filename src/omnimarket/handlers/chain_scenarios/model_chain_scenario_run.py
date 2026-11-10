# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one scenario run observed."""

from __future__ import annotations

from omnibase_core.enums.enum_chain_scenario_reading import EnumChainScenarioReading
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.chain_scenarios.model_chain_scenario_observed_event import (
    ModelChainScenarioObservedEvent,
)


class ModelObservedLedgerRow(BaseModel):
    """A ledger failure row the observation relay wrote."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_type: str
    subject: str
    correlation_id: str
    failure: str


class ModelChainScenarioRun(BaseModel):
    """Events in order, the ledger rows and readings written, and the dependency evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    events: tuple[ModelChainScenarioObservedEvent, ...] = ()
    ledger_rows: tuple[ModelObservedLedgerRow, ...] = ()
    readings: dict[str, EnumChainScenarioReading] = Field(default_factory=dict)
    dependency_calls: dict[str, int] = Field(default_factory=dict)
    state_writes: tuple[tuple[str, str, str], ...] = ()
    handler_error: str | None = None
    timed_out: bool = False


__all__ = ["ModelChainScenarioRun", "ModelObservedLedgerRow"]
