# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of the seam row store: append and read, with no way to change a stored row."""

from __future__ import annotations

from omnimarket.nodes.node_seam_reading_compute.protocols.protocol_seam_row_store import (
    ProtocolSeamRowStore,
)

__all__ = ["ProtocolSeamRowStore"]
