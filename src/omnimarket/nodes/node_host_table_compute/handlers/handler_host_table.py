# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn the lab host table into the row set every reader uses.

Which hosts are admitted and which are excluded, each exclusion with its stated reason. The node
is pure: the caller reads the table (``resolve_lab_hosts_table``) and passes it in; this handler
does no I/O.
"""

from __future__ import annotations

from ..models.model_host_table import (
    ModelHostTableRequest,
    ModelHostTableResult,
    ModelHostTableRow,
)


class HostTableRefusedError(ValueError):
    """The table could not be turned into a row set."""


class HandlerHostTable:
    """COMPUTE handler: the lab host table to its admitted and excluded rows."""

    def handle(self, request: ModelHostTableRequest) -> ModelHostTableResult:
        rows = tuple(
            ModelHostTableRow(
                name=host.name,
                cap=host.cap,
                available=host.available,
                exclusion_reason=""
                if host.available
                else (host.unavailable_reason or "").strip(),
            )
            for host in request.table.hosts
        )
        unreasoned = sorted(
            row.name for row in rows if not row.available and not row.exclusion_reason
        )
        if unreasoned:
            raise HostTableRefusedError(
                f"lab host table: {', '.join(unreasoned)} "
                "excluded without a stated reason"
            )
        seen: set[str] = set()
        duplicated: list[str] = []
        for row in rows:
            if row.name in seen and row.name not in duplicated:
                duplicated.append(row.name)
            seen.add(row.name)
        if duplicated:
            raise HostTableRefusedError(
                f"lab host table: duplicate host names: {', '.join(duplicated)}"
            )
        return ModelHostTableResult(
            rows=rows,
            admitted=tuple(row.name for row in rows if row.available),
            excluded=tuple(row for row in rows if not row.available),
        )
