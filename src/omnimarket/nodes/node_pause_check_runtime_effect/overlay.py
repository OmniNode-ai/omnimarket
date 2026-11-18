# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_pause_check_runtime_effect: every deployment fact, none in code.

Found at ``<root>/node_pause_check_runtime_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_PAUSE_CHECK_OVERLAY`` names. The package ships
``overlay.example.yaml``. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.pause_check import (
    ModelPauseCheckOverlay,
)

NODE_NAME = "node_pause_check_runtime_effect"
OVERLAY_FILE_ENV = "ONEX_PAUSE_CHECK_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelPauseCheckOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelPauseCheckOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
