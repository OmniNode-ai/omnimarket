# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_overlay_merge_order_compute: every setting of the check, none in code.

Found at ``<root>/node_overlay_merge_order_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it, and a missing overlay
refuses the run.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    EnumOverlayVerdict,
)

NODE_NAME = "node_overlay_merge_order_compute"


class ModelOverlayMergeOrderOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The merged tree's verdicts that mean an overlay is ahead of its code; "accepted" is never listed.
    ahead_verdicts: tuple[EnumOverlayVerdict, ...] = Field(min_length=1)
    #: The most ahead overlays the decided event names; the count is always the full number.
    max_named: int = Field(ge=1)
    #: The fault injected on purpose that must turn the decision AHEAD_OF_CODE.
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _accepted_is_the_pass(self) -> ModelOverlayMergeOrderOverlay:
        if EnumOverlayVerdict.ACCEPTED in self.ahead_verdicts:
            raise ValueError("accepted is the passing verdict and is never ahead")
        if len(set(self.ahead_verdicts)) != len(self.ahead_verdicts):
            raise ValueError("a verdict is listed twice")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelOverlayMergeOrderOverlay:
    return load_structured_overlay(NODE_NAME, ModelOverlayMergeOrderOverlay, environ)
