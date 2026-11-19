# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``node_oneoff_register_compute`` overlay: the register of one-offs, none in code.

Shape only; the register belongs to the deployment owner and is read from
``<root>/node_oneoff_register_compute/overlay.yaml``. The liveness refs are read the same way
from the ``automation_liveness`` overlay.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_oneoff_register_compute.models.model_oneoff_register import (
    ModelLivenessOverlayRefs,
    ModelOneOffRegisterPolicy,
)

OVERLAY_ID = "node_oneoff_register_compute"
OVERLAY_ID_LIVENESS = "automation_liveness"


def load_oneoff_register_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelOneOffRegisterPolicy:
    """Load and validate the register overlay; a missing key or a bad value is refused naming it."""
    return load_structured_overlay(OVERLAY_ID, ModelOneOffRegisterPolicy, environ)


def load_liveness_refs(
    environ: Mapping[str, str] | None = None,
) -> ModelLivenessOverlayRefs:
    """Load and validate the liveness overlay; a missing key or a bad value is refused naming it."""
    return load_structured_overlay(
        OVERLAY_ID_LIVENESS, ModelLivenessOverlayRefs, environ
    )
