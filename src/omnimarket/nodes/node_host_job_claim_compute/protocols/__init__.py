# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_host_job_claim_compute: where the installed units come from."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_host_job_claim_compute.models import ModelInstalledUnit


class ProtocolInstalledUnits(Protocol):
    """Read-only view of the units installed on a host."""

    def installed(
        self,
        unit_dirs: tuple[str, ...],
        suffix: str,
    ) -> tuple[ModelInstalledUnit, ...]:
        """The installed units, sorted by name, names unique (the first directory wins)."""
        ...


__all__ = ["ProtocolInstalledUnits"]
