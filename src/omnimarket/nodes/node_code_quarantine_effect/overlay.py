# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_code_quarantine_effect: every deployment fact, none in code.

Found at ``<root>/node_code_quarantine_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_CODE_QUARANTINE_OVERLAY`` names. The package ships
``overlay.example.yaml`` with synthetic values on the in-memory backend. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_code_quarantine_effect.models import ModelQuarantineOverlay

NODE_NAME = "node_code_quarantine_effect"
OVERLAY_FILE_ENV = "ONEX_CODE_QUARANTINE_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelQuarantineOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelQuarantineOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
