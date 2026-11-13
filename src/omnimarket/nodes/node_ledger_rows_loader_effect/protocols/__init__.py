# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected dependencies of the ledger rows loader."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    ModelRowsLoaderEvent,
)


class ProtocolRowsLoaderEventSink(Protocol):
    def emit(self, event: ModelRowsLoaderEvent) -> None: ...

    def fallback_reads(self) -> int:
        """How many file-fallback answers were emitted before now (the cutover measure)."""
        ...


class ProtocolFileRows(Protocol):
    def read(self) -> tuple[str, ...]:
        """The ledger file's rows of the window; raises OSError when the file cannot be read."""
        ...


__all__ = ["ProtocolFileRows", "ProtocolRowsLoaderEventSink"]
