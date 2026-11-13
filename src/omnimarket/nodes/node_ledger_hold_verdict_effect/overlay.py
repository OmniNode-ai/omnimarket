# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_ledger_hold_verdict_effect: every deployment fact, none in code.

Found at ``<root>/node_ledger_hold_verdict_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or at the file ``ONEX_LEDGER_HOLD_VERDICT_OVERLAY`` names. The
package ships ``overlay.example.yaml``. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.events.hold_verdict import (
    ModelHoldVerdictOverlay,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_ledger_hold_verdict_effect"
OVERLAY_FILE_ENV = "ONEX_LEDGER_HOLD_VERDICT_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelHoldVerdictOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelHoldVerdictOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
