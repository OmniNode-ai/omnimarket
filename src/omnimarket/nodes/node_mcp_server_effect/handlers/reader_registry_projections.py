# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Factory the node contract names for its registry reader (``config.registry_factory``).

The runtime hands ``HandlerMcpToolsList`` no argument, so the handler asks the contract
for this factory. The database address is read from the environment variable the contract
names (``config.projection_dsn_env``); nothing here types a host, port or credential.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from typing import Any

from omnimarket.handlers.container.container_overlay_dependencies import (
    packaged_contract_value,
)
from omnimarket.nodes.node_mcp_server_effect.handlers.handler_mcp_registry_projection_rows import (
    HandlerMcpRegistryProjectionRows,
)
from omnimarket.nodes.node_mcp_server_effect.models.refusal import (
    EnumMcpServerRefusal,
    McpServerRefusalError,
)

PACKAGE = "omnimarket.nodes.node_mcp_server_effect"


async def _read_every_projection(dsn: str) -> list[Any]:
    import asyncpg
    from omnibase_infra.enums import EnumRegistrationState
    from omnibase_infra.projectors.projection_reader_registration import (
        ProjectionReaderRegistration,
    )

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        reader = ProjectionReaderRegistration(pool)
        counts = await reader.count_by_state()
        projections: list[Any] = []
        for state in EnumRegistrationState:
            size = counts.get(state, 0)
            if size:
                projections += await reader.get_by_state(state, limit=size)
        return projections
    finally:
        await pool.close()


def _projections_from_environment() -> Sequence[Any]:
    env_name = str(packaged_contract_value(PACKAGE, "config", "projection_dsn_env"))
    dsn = os.environ.get(env_name)
    if not dsn:
        raise McpServerRefusalError(
            EnumMcpServerRefusal.REGISTRY_UNREADABLE,
            f"the registry address is not set: environment variable {env_name} is empty",
        )
    return asyncio.run(_read_every_projection(dsn))


def build_registry_reader(
    projections: Callable[[], Sequence[Any]] | None = None,
) -> HandlerMcpRegistryProjectionRows:
    """The projection-backed reader; ``projections`` replaces the database read in tests."""
    return HandlerMcpRegistryProjectionRows(
        projections if projections is not None else _projections_from_environment
    )
