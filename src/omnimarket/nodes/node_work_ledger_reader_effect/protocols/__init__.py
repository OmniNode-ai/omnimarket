# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the moved ledger reader."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderEvent,
)


class ProtocolLedgerPointRead(Protocol):
    """One request to the bus-fed work ledger query node; never raises, an error is an answer."""

    def read(self, query: ModelPointQuery) -> ModelPointAnswer: ...


class ProtocolReaderEventSink(Protocol):
    def emit(self, event: ModelReaderEvent) -> None: ...


__all__ = [
    "ModelPointQuery",
    "ProtocolLedgerPointRead",
    "ProtocolReaderEventSink",
]
