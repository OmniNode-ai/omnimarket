# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every host that takes lanes receives the snapshot: the lab host table decides who they are (OMN-23219).

The publisher used to copy the snapshot only to the hosts its caller named, and the unit's own list named
three. A lane placed on any other host (h053 and h051 among them) found no snapshot, and the
private-repo-runner-placement hook refused every workflow commit it made in a private repository
(hook-preflight exit 3, nothing committed). The lab host table (the ``lab_hosts`` overlay's projection)
is the one place that says which hosts take lanes, so the publisher reads that table and adds every such
host to the caller's list. A host the caller names keeps the caller's row, so nothing that worked changes.

A row takes lanes when it is not a launcher, not a contributor's machine (nothing is written to a
collaborator's host) and its ``cap`` is not 0 (0 places no lane on it, OMN-22015). Availability is
the publisher's own concern: an unavailable row is returned as unavailable and reported ``skipped`` with
the table's reason, so a host that comes back is covered by the next run without any edit.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)

from ..models import ModelPrrpSnapshotHost


def takes_lanes(row: ModelLabHostRow) -> bool:
    """Whether the table places lanes on this row: the rule of the table's own columns."""
    return not row.contributor and "launcher" not in (row.roles or ()) and row.cap != 0


def lane_hosts(table: ModelLabHostsTable) -> tuple[ModelPrrpSnapshotHost, ...]:
    """One publisher host per table row that takes lanes, in table order."""
    return tuple(
        ModelPrrpSnapshotHost(
            name=row.name,
            target=row.target,
            local=row.local,
            available=row.available,
            unavailable_reason=row.unavailable_reason or "",
        )
        for row in table.hosts
        if takes_lanes(row)
    )


def merge_hosts(
    declared: tuple[ModelPrrpSnapshotHost, ...],
    from_table: tuple[ModelPrrpSnapshotHost, ...],
) -> tuple[ModelPrrpSnapshotHost, ...]:
    """The declared hosts as given, then each table host whose name and target the caller did not name."""
    names = {host.name for host in declared}
    targets = {host.target for host in declared}
    extra = tuple(
        host
        for host in from_table
        if host.name not in names and host.target not in targets
    )
    return (*declared, *extra)


def resolve_hosts(
    declared: tuple[ModelPrrpSnapshotHost, ...],
    read_table: Callable[[], ModelLabHostsTable],
) -> tuple[tuple[ModelPrrpSnapshotHost, ...], tuple[str, ...]]:
    """The hosts to publish to and the problems reading the table.

    A table that cannot be read leaves the caller's hosts in place (they still get the file) and comes
    back as a problem, which fails the run: a publisher that silently fell back to its short list is the
    defect this module closes."""
    try:
        table = read_table()
    except (
        Exception
    ) as error:  # the overlay reader raises its own typed errors; none may end the run
        return declared, (
            f"the lab host table could not be read, so only the named hosts are covered ({error})",
        )
    return merge_hosts(declared, lane_hosts(table)), ()
