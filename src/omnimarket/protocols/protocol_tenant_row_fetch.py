# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injectable tenant-bound row read, so a source is tested without a database (OMN-20577)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolTenantRowFetch(Protocol):
    """Run one parameterised SELECT under the tenant this reader was bound to.

    An implementation resolves its tenant once, at construction, and every call runs
    under that tenant only: the statement text and the parameters cannot name another.
    """

    def fetch(
        self, table: str, sql: str, params: tuple[object, ...]
    ) -> list[dict[str, object]]:
        """Return the rows of ``sql`` as dicts; an empty list when the tenant holds none."""
        ...
