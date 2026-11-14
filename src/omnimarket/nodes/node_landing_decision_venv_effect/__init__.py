# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installs every dependency the landing decision pin names into the decision venv (OMN-24355)."""

from omnimarket.nodes.node_landing_decision_venv_effect.handlers.handler_landing_decision_venv import (
    HandlerLandingDecisionVenv,
)

__all__ = ["HandlerLandingDecisionVenv"]
