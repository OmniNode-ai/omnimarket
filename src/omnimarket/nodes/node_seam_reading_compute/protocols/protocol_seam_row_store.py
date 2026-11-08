# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam row store port: append-only, so it declares no update and no delete."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    EnumRowAppendStatus,
    ModelSeamRow,
)


class ProtocolSeamRowStore(Protocol):
    """Append per-seam rows and read them back in append order.

    ``append`` of a key already stored with identical content is ``already-appended``; with
    different content it is ``refused-rewrite`` and the stored row is left as it was.
    """

    def append(self, row: ModelSeamRow) -> EnumRowAppendStatus: ...

    def rows(self, seam: str) -> tuple[ModelSeamRow, ...]: ...


__all__ = ["ProtocolSeamRowStore"]
