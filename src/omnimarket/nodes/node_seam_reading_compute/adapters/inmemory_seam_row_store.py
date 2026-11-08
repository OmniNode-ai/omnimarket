# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory seam row store, the default backend: append-only."""

from __future__ import annotations

from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    EnumRowAppendStatus,
    ModelSeamRow,
)


class InMemorySeamRowStore:
    """Rows by seam in append order, and a key index that decides a rewrite."""

    def __init__(self) -> None:
        self._by_key: dict[str, ModelSeamRow] = {}
        self._by_seam: dict[str, list[ModelSeamRow]] = {}

    def append(self, row: ModelSeamRow) -> EnumRowAppendStatus:
        stored = self._by_key.get(row.key)
        if stored is not None:
            return (
                EnumRowAppendStatus.ALREADY_APPENDED
                if stored == row
                else EnumRowAppendStatus.REFUSED_REWRITE
            )
        self._by_key[row.key] = row
        self._by_seam.setdefault(row.seam, []).append(row)
        return EnumRowAppendStatus.APPENDED

    def rows(self, seam: str) -> tuple[ModelSeamRow, ...]:
        return tuple(self._by_seam.get(seam, ()))


__all__ = ["InMemorySeamRowStore"]
