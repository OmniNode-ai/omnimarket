# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_fable_review_gate_compute: the ruling's gate settings, none in code.

Found at ``<root>/node_fable_review_gate_compute/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key
refuses the load. The host identity itself never stands in the overlay: ``host_name_env`` names the
environment variable that holds this machine's host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_fable_review_gate_compute.models.model_fable_review_gate import (
    ModelFableReviewGateOverlay,
)

NODE_NAME = "node_fable_review_gate_compute"


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelFableReviewGateOverlay:
    return load_structured_overlay(NODE_NAME, ModelFableReviewGateOverlay, environ)


def read_origin_host(
    overlay: ModelFableReviewGateOverlay,
    environ: Mapping[str, str] | None = None,
) -> str:
    """The host the review would run on, read from the variable the overlay names.

    An absent or blank variable reads as the empty string: an unreadable origin, which the gate refuses
    with the typed reason.
    """
    env = os.environ if environ is None else environ
    return env.get(overlay.host_name_env, "").strip()
