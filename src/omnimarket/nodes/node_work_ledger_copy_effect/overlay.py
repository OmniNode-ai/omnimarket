# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_work_ledger_copy_effect: every deployment fact, none in code.

Found at ``<root>/node_work_ledger_copy_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_LEDGER_COPY_OVERLAY`` names. The package ships
``overlay.example.yaml`` with synthetic values. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_work_ledger_copy_effect.models import ModelCopyOverlay

NODE_NAME = "node_work_ledger_copy_effect"
OVERLAY_FILE_ENV = "ONEX_LEDGER_COPY_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelCopyOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelCopyOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
