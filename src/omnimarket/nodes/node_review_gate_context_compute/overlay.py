# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_review_gate_context_compute: the declared review-gate context policy, none in code.

Found at ``<root>/node_review_gate_context_compute/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses
the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_review_gate_context import ModelReviewGateContextPolicy

NODE_NAME = "node_review_gate_context_compute"


class ModelReviewGateContextOverlay(BaseModel):
    """The overlay of the review-gate context node: the declared policy of the check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelReviewGateContextPolicy


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelReviewGateContextOverlay:
    return load_structured_overlay(NODE_NAME, ModelReviewGateContextOverlay, environ)


__all__ = ["NODE_NAME", "ModelReviewGateContextOverlay", "load_overlay"]
