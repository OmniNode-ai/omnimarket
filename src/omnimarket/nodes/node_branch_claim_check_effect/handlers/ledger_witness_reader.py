# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus witness for parity measurement, never for holder resolution.

The rows come from the bus rows loader (``node_ledger_rows_loader_effect``) through an injected
reader. A loader that does not answer raises ``LedgerRowsUnreadableError``; the ledger file is
never opened in its place.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from omnimarket.handlers.ledger_rows_via_node import (
    read_ledger_rows_via_node,
    window_hours_covering,
)
from omnimarket.nodes.node_branch_claim_check_effect.models.model_branch_claim_policy import (
    ModelParityWitness,
)


class ProtocolLedgerWitness(Protocol):
    def read_rows(self) -> list[str]: ...


class BusLedgerWitness:
    def __init__(
        self,
        source: ModelParityWitness,
        *,
        since: datetime,
        now: datetime,
        rows_reader: Callable[[str, int], tuple[str, ...]] = read_ledger_rows_via_node,
    ) -> None:
        self._source = source
        self._since = since
        self._now = now
        self._rows_reader = rows_reader

    def read_rows(self) -> list[str]:
        hours = window_hours_covering(self._since, self._now)
        rows = self._rows_reader(self._source.reader, hours)
        return [row for row in rows if row.strip()]
