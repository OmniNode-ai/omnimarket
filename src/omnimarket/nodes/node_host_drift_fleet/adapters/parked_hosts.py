# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parked-host ports of node_host_drift_fleet: the real lab host table, in memory for tests.

The real source reads the lab host table the overlay store serves; it names no host, path
or repository in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table


class LabHostTableParkedSource:
    """The parked hosts of the lab host table (``available: false``), with the table's reason."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    def parked_hosts(self) -> Mapping[str, str]:
        table = resolve_lab_hosts_table(self._environ)
        return {
            row.name: row.unavailable_reason or "parked"
            for row in table.hosts
            if not row.available
        }


class InMemoryParkedSource:
    """A fixed parked map; ``error`` makes the read fail the way an unreadable table does."""

    def __init__(self, parked: Mapping[str, str], error: str | None = None) -> None:
        self._parked = dict(parked)
        self.error = error

    def parked_hosts(self) -> Mapping[str, str]:
        if self.error is not None:
            raise ValueError(self.error)
        return dict(self._parked)


__all__ = [
    "InMemoryParkedSource",
    "LabHostTableParkedSource",
]
