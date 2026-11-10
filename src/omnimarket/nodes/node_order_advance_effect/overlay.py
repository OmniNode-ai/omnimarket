# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``order_advance`` overlay: the writer's backend and comparison window, none in code.

Shape only; the backend (``run_engine_backend``) and the length of the comparison belong to the
deployment owner and are read from ``<root>/order_advance/overlay.yaml``. The order itself is the
``gate_store`` overlay's.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_order_advance import ModelOrderAdvancePolicy

OVERLAY_ID = "order_advance"


def load_order_advance_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelOrderAdvancePolicy:
    """Load and validate the overlay; a missing key or a bad value is refused naming it."""
    return load_structured_overlay(OVERLAY_ID, ModelOrderAdvancePolicy, environ)
