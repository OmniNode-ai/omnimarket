# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory storage backend: simulated hosts with a free-bytes counter each."""

from __future__ import annotations

import posixpath

from omnimarket.nodes.node_host_storage_effect.models.model_host_storage import (
    EnumHostStorageEntryOutcome,
    ModelHostStorageEntryResult,
    ModelHostStorageEntryRun,
    ModelHostStorageRun,
)
from omnimarket.nodes.node_host_storage_effect.models.model_host_storage_overlay import (
    ModelHostStorageOverlay,
    ModelStorageCommand,
    ModelStorageHost,
)


class BackendHostStorageInMemory:
    def __init__(self, overlay: ModelHostStorageOverlay) -> None:
        self._free = {h.name: h.simulated_free_bytes for h in overlay.hosts}
        # host -> entry path -> (size, in use); a simulated entry leaves the table when removed.
        self._entries = {
            h.name: {e.path: (e.size_bytes, e.in_use) for e in h.simulated_entries}
            for h in overlay.hosts
        }
        self.ran: list[tuple[str, str]] = []
        #: Hosts whose reading is unavailable, for the error chain.
        self.unreadable: set[str] = set()
        #: Commands that exit non-zero, for the error chain.
        self.failing: set[str] = set()

    def free_bytes(self, host: ModelStorageHost, path: str) -> int | None:
        if host.name in self.unreadable:
            return None
        return self._free[host.name]

    def run(
        self, host: ModelStorageHost, command: ModelStorageCommand
    ) -> ModelHostStorageRun:
        self.ran.append((host.name, command.name))
        if command.name in self.failing:
            return ModelHostStorageRun(exit_code=1, output_tail="simulated failure")
        self._free[host.name] = max(
            0, self._free[host.name] + command.simulated_effect_bytes
        )
        return ModelHostStorageRun(exit_code=0)

    def remove_entries(
        self,
        host: ModelStorageHost,
        command: ModelStorageCommand,
        entries: tuple[str, ...],
    ) -> ModelHostStorageEntryRun:
        """Simulated entries only: a lexical root check, a simulated in-use flag, a simulated size."""
        roots = host.roots_for(command.name)
        table = self._entries[host.name]
        rows: list[ModelHostStorageEntryResult] = []
        for entry in entries:
            normal = posixpath.normpath(entry)
            inside = posixpath.isabs(entry) and any(
                normal.startswith(root + "/") for root in roots
            )
            if not inside:
                rows.append(
                    ModelHostStorageEntryResult(
                        entry=entry,
                        outcome=EnumHostStorageEntryOutcome.REFUSED_OUTSIDE_ROOT,
                        detail="not strictly inside a declared root",
                    )
                )
            elif normal not in table:
                rows.append(
                    ModelHostStorageEntryResult(
                        entry=entry,
                        outcome=EnumHostStorageEntryOutcome.REFUSED_NOT_FOUND,
                        detail="no such entry",
                    )
                )
            elif table[normal][1]:
                rows.append(
                    ModelHostStorageEntryResult(
                        entry=entry,
                        outcome=EnumHostStorageEntryOutcome.REFUSED_IN_USE,
                        detail="simulated holder",
                    )
                )
            else:
                size = table.pop(normal)[0]
                self._free[host.name] += size
                self.ran.append((host.name, command.name))
                rows.append(
                    ModelHostStorageEntryResult(
                        entry=entry,
                        outcome=EnumHostStorageEntryOutcome.REMOVED,
                        freed_bytes=size,
                    )
                )
        return ModelHostStorageEntryRun(entry_results=tuple(rows))
