# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus appender of the tick's STATUS row: a protocol with no ledger path, the bus command and an in-memory fake (OMN-26329).

The file appender in ``protocol_ledger_appender`` stays for the nodes that still import it; this tick appends only here."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)


class ModelBusAppendOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    detail: str


class ProtocolBusLedgerAppender(Protocol):
    def append(self, row: str) -> ModelBusAppendOutcome:
        """Append one row to the ledger and report whether it was accepted."""


class BusCommandLedgerAppender:
    """Appends through the bus append command the unit's environment names; the ledger file is never named.

    The request id is derived from the row, so a row retried on the next tick is one request the ledger host answers once.
    """

    def __init__(
        self,
        command: Sequence[str],
        lane: str,
        host: str,
        timeout_seconds: int,
    ) -> None:
        self._command = list(command)
        self._lane = lane
        self._host = host
        self._timeout = timeout_seconds

    def append(self, row: str) -> ModelBusAppendOutcome:
        outcome = CommandLedgerAppender(
            self._command, self._lane, self._host, self._timeout
        ).append(row, str(uuid5(NAMESPACE_URL, f"onex:friction-rollup-tick:{row}")))
        accepted = outcome.status in ("accepted", "duplicate")
        return ModelBusAppendOutcome(
            ok=accepted, detail=f"{outcome.status}: {outcome.message}".strip()
        )


class InMemoryBusLedgerAppender:
    """Keeps appended rows in a list; ``refuse`` makes every append fail with that detail."""

    def __init__(self, refuse: str | None = None) -> None:
        self.rows: list[str] = []
        self._refuse = refuse

    def append(self, row: str) -> ModelBusAppendOutcome:
        if self._refuse is not None:
            return ModelBusAppendOutcome(ok=False, detail=self._refuse)
        self.rows.append(row)
        return ModelBusAppendOutcome(ok=True, detail="rc=0")
