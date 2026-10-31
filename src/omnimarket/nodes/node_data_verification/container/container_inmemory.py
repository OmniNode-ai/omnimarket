# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory data source the verification entry point runs against (OMN-21695).

The handler never constructs the data source itself: it takes the one this module binds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.nodes.node_data_verification.handlers.handler_data_verification import (
        InmemoryDataSource,
    )


def bind_data_source() -> InmemoryDataSource:
    """Bind a fresh in-memory data source with no rows."""
    from omnimarket.nodes.node_data_verification.handlers.handler_data_verification import (
        InmemoryDataSource,
    )

    return InmemoryDataSource()
