# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``gate_store`` overlay: every setting of the gate store, none in code.

Shape only; the backend, the per-piece limits, the reservation lifetime and the order belong
to the deployment owner and are read from ``<root>/gate_store/overlay.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_gate_store import ModelGateStorePolicy

OVERLAY_ID = "gate_store"


def load_gate_store_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelGateStorePolicy:
    """Load and validate the overlay; refuses a limit below 1 or an order naming no limit."""
    policy = load_structured_overlay(OVERLAY_ID, ModelGateStorePolicy, environ)
    for piece, limit in policy.limits.items():
        if limit < 1:
            raise NodeOverlayError(f"{OVERLAY_ID}.limits.{piece} must be at least 1")
    if policy.backend.value == "postgres" and policy.postgres is None:
        raise NodeOverlayError(
            f"{OVERLAY_ID}.postgres is required when backend is postgres"
        )
    for piece in policy.order:
        if piece not in policy.limits:
            raise NodeOverlayError(
                f"{OVERLAY_ID}.order names {piece}, which has no limit"
            )
    return policy
