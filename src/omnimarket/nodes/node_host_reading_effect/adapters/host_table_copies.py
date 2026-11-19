# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host table copies drift node's overlay and its local copy source, for the host-table-copies probe."""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnimarket.nodes.node_host_table_copies_drift_compute.adapters.copy_source import (
    LocalCopySource,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import (
    ModelHostTableCopiesDriftOverlay,
    load_overlay,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.protocols import (
    ProtocolCopySource,
)


class DriftNodeCopies:
    """The drift node's own loader and its ``LocalCopySource``: the copies are on this host's disk."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def overlay(self) -> ModelHostTableCopiesDriftOverlay:
        return load_overlay(self._environ)

    def source(self, overlay: ModelHostTableCopiesDriftOverlay) -> ProtocolCopySource:
        return LocalCopySource(overlay.copies, self._environ)
