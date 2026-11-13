# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injectable tenant-bound table read, so a source is tested without a database (OMN-20577)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolTenantRowQuery(Protocol):
    """Read rows of one table under the tenant this reader was bound to.

    An implementation resolves its tenant at construction and sets ``app.tenant_id`` before
    every read, so the relation's RLS policy limits the rows to that tenant.
    """

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        """Return the rows of ``table`` matching the equality ``filters``."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...
