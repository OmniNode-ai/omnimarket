# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_ask_done_check_effect, under ONEX_SKILL_OVERLAY_ROOTS or the file
ONEX_ASK_DONE_CHECK_OVERLAY names. No key has a default."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_ask_done_check_effect.models import ModelAskDoneCheckOverlay

NODE_NAME = "node_ask_done_check_effect"
OVERLAY_FILE_ENV = "ONEX_ASK_DONE_CHECK_OVERLAY"


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelAskDoneCheckOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelAskDoneCheckOverlay, environ, file_env=OVERLAY_FILE_ENV
    )
