# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_dev_roll_failure_capture_effect: the capture settings, none in code.

Found at ``<root>/node_dev_roll_failure_capture_effect/overlay.yaml`` under a root named
in ``ONEX_SKILL_OVERLAY_ROOTS``. Nothing here has a default: an absent key refuses the
load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_dev_roll_failure_capture_effect"


class ModelDevRollCaptureSettings(BaseModel):
    """Where the captured logs and terminal records are written, and their bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_root: str = Field(min_length=1)
    retention_days: int = Field(gt=0)
    log_byte_cap: int = Field(gt=0)


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelDevRollCaptureSettings:
    """Read ``<root>/node_dev_roll_failure_capture_effect/overlay.yaml``; absent refuses."""
    return load_structured_overlay(NODE_NAME, ModelDevRollCaptureSettings, environ)


__all__ = [
    "NODE_NAME",
    "ModelDevRollCaptureSettings",
    "load_settings",
]
