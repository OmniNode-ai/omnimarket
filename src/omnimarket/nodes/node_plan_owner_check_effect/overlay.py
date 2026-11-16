# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_plan_owner_check_effect: every deployment fact, none in code.

Found at ``<root>/node_plan_owner_check_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_PLAN_OWNER_CHECK_OVERLAY`` names. The
package ships ``overlay.example.yaml``. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.plan_owner_check import (
    ModelPlanOwnerOverlay,
)

NODE_NAME = "node_plan_owner_check_effect"
OVERLAY_FILE_ENV = "ONEX_PLAN_OWNER_CHECK_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelPlanOwnerOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelPlanOwnerOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
