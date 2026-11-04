# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The database handle ``node_projection_write_effect`` is constructed with (OMN-18883).

``omnimarket.adapters.asyncpg_adapter.AsyncpgAdapter`` satisfies it. The handle
reaches the write handler through its constructor, never through a payload key.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ProtocolProjectionWriteDatabase"]


@runtime_checkable
class ProtocolProjectionWriteDatabase(Protocol):
    """Run one statement in its own transaction and return the rows it returned."""

    async def execute(
        self,
        query: str,
        *params: Any,
        tenant: str | None = None,
    ) -> list[dict[str, Any]]: ...
