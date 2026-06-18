# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Importable PR-merged projection handler shell.

The durable projection behavior lands in OMN-13227. OMN-13226 still declares
the route so contract and routing validators can resolve a concrete class.
"""

from __future__ import annotations

from typing import Any


class HandlerPrMergedProjection:
    """Concrete contract target for the PR-merged projection route."""

    def handle(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return a deterministic no-op projection result until OMN-13227."""
        return {
            "projected": False,
            "reason": "projection-handler-pending-omn-13227",
            "payload_keys": sorted((payload or {}).keys()),
        }
