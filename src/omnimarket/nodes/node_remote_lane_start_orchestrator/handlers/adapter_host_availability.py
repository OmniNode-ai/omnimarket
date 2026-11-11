# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which hosts the lab host table takes out of placement, read for the lane start (OMN-22847).

The placement node judges the readings it is given; a reading carries a ``lane_admission_refusal`` only when the
caller puts one there. The lab host table already says which hosts are out (``available: false`` with its
``unavailable_reason``), and every other placement reader honours it (``node_lab_placement_compute``, the floor
record's host status). This adapter reads the same table through the same loader and hands the start orchestrator
the hosts to refuse, so a lane is never placed on a host the table says is out.

The table is read at each start (a host put out of service after the leader began is seen at the next start). A
table that cannot be read is an error the start refuses on: it never places as if every host were in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_placement import (
    load_host_table,
)

#: Host name -> the table's reason for keeping the host out of placement.
HostAvailability = Callable[[], Mapping[str, str]]

NO_REASON = "no reason recorded"


class HostTableUnreadableError(RuntimeError):
    """The lab host table could not be read, so no host can be judged."""


def lab_host_table_unavailable(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The hosts the lab host table marks ``available: false``, each with its ``unavailable_reason``."""
    try:
        table = load_host_table(environ)
    except ValueError as exc:
        raise HostTableUnreadableError(str(exc)) from exc
    return {
        row.name: row.unavailable_reason or NO_REASON
        for row in table.hosts
        if not row.available
    }


__all__ = [
    "NO_REASON",
    "HostAvailability",
    "HostTableUnreadableError",
    "lab_host_table_unavailable",
]
