# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``node_ruling_register_compute`` overlay: the ruling register, none in code.

Shape only; the register belongs to the deployment owner and is read from
``<root>/node_ruling_register_compute/overlay.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_ruling_register import ModelRulingRegisterPolicy

OVERLAY_ID = "node_ruling_register_compute"


def load_ruling_register_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelRulingRegisterPolicy:
    """Load and validate the register overlay; a missing key or a bad value is refused naming it."""
    return load_structured_overlay(OVERLAY_ID, ModelRulingRegisterPolicy, environ)


__all__ = ["OVERLAY_ID", "load_ruling_register_policy"]
