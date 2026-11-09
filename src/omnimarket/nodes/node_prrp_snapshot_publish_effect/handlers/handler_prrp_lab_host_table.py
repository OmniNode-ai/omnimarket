# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The snapshot publisher's target hosts: every row of the declared lab host table (OMN-23396).

The host list lives in one place, the ``lab_hosts`` overlay, read through ``resolve_lab_hosts_table``
(the pinned table file, the overlay roots, then the declared workspace overlays). The publisher keeps no
host list of its own, so a host added to the table is written on the next run and a host removed from it is
not; a host the table marks unavailable is skipped with the table's reason.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostsTable

from ..models import ModelPrrpSnapshotHost


def hosts_from_table(table: ModelLabHostsTable) -> tuple[ModelPrrpSnapshotHost, ...]:
    """One publisher host per table row, carrying the row's ssh route."""
    return tuple(
        ModelPrrpSnapshotHost(
            name=row.name,
            target=row.target,
            local=row.local,
            available=row.available,
            unavailable_reason=row.unavailable_reason or "",
            fallback_targets=tuple(row.fallback_targets),
            ssh_user=row.ssh_user,
            proxy_jump=row.proxy_jump,
            ssh_identity=row.ssh_identity,
            fallback_ssh_user=row.fallback_ssh_user,
            fallback_proxy_jump=row.fallback_proxy_jump,
            fallback_ssh_identity=row.fallback_ssh_identity,
        )
        for row in table.hosts
    )


def load_lab_hosts(
    environ: Mapping[str, str] | None = None,
) -> tuple[ModelPrrpSnapshotHost, ...]:
    """The hosts of the declared lab host table; raises NodeOverlayError when no source supplies a valid table."""
    return hosts_from_table(resolve_lab_hosts_table(environ))
