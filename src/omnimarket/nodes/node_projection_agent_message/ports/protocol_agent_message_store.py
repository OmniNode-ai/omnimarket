# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the ingest handler asks of the store that holds the rows."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_projection_agent_message.models import ModelAgentMessageRow


class ProtocolAgentMessageStore(Protocol):
    async def put(self, row: ModelAgentMessageRow) -> bool:
        """Keep the row; False when the partition and offset already have one (the first row is kept)."""
        ...
