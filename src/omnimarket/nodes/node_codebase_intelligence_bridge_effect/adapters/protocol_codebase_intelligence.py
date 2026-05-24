# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SPI protocol boundary for codebase intelligence providers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProtocolCodebaseIntelligence(Protocol):
    """Structural protocol for codebase intelligence backend adapters.

    Any adapter (Repowise CLI, stub, future providers) must satisfy this
    interface. The handler only depends on this protocol — never on the
    concrete adapter class.
    """

    async def query(
        self,
        operation: str,
        query: str,
        targets: tuple[str, ...],
        include: tuple[str, ...],
    ) -> dict[str, Any]:
        """Execute a codebase intelligence operation.

        Returns a raw dict matching the provider JSON contract.
        Must raise ``asyncio.TimeoutError`` on timeout.
        """
        ...


__all__ = ["ProtocolCodebaseIntelligence"]
