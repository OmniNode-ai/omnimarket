# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding site of the in-memory data source the verification entry point runs against (OMN-21695).

The handler never constructs the data source itself: it takes the one this module binds.
"""

from __future__ import annotations


class InmemoryDataSource:
    """Mock data source for testing."""

    def __init__(self, rows: list[dict[str, str]] | None = None) -> None:
        self._rows = rows or []

    def get_row_count(self, table_name: str) -> int:
        return len(self._rows)

    def get_sample_rows(
        self, table_name: str, sample_size: int
    ) -> list[dict[str, str]]:
        return self._rows[:sample_size]

    def get_columns(self, table_name: str) -> list[str]:
        if not self._rows:
            return []
        return list(self._rows[0].keys())


def bind_data_source() -> InmemoryDataSource:
    """Bind a fresh in-memory data source with no rows."""
    return InmemoryDataSource()
