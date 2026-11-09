# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only row access a catalog repository binding needs from its database pool."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProtocolCatalogRowReader(Protocol):
    """Run one read-only statement and return its rows as ordered tuples."""

    async def fetch(self, sql: str) -> Sequence[Sequence[Any]]: ...


__all__: list[str] = ["ProtocolCatalogRowReader"]
