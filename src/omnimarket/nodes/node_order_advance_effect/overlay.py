# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``order_writer`` overlay: every setting of the order writer, none in code.

Shape only; the holder name, the step-key prefix, the run engine backend, the readiness read and
the takeover rule belong to the deployment owner and are read from
``<root>/order_writer/overlay.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_order_advance import ModelOrderWriterPolicy

OVERLAY_ID = "order_writer"


def load_order_writer_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelOrderWriterPolicy:
    """Load and validate the overlay; an absent or unknown key refuses and names itself."""
    return load_structured_overlay(OVERLAY_ID, ModelOrderWriterPolicy, environ)
