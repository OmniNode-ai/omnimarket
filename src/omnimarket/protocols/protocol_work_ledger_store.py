# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""LED: the work ledger a node appends its decision rows to.

Only the append the release gate needs (OMN-21493). The remaining reads and the
conformance suite are added by the ledger-store task (T-B3). A store raises when the
row was not written; the caller never reports a decision it could not record as admitted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolWorkLedgerStore(Protocol):
    """Appends one typed row: a row kind and its string fields."""

    def append_row(self, kind: str, fields: Mapping[str, str]) -> None: ...


__all__: list[str] = ["ProtocolWorkLedgerStore"]
