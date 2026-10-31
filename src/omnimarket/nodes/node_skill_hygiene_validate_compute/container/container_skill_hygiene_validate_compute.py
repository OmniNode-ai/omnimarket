# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_skill_hygiene_validate_compute."""

from __future__ import annotations

from omnimarket.nodes.node_skill_hygiene_validate_compute.handlers.handler_skill_hygiene_validate import (
    HandlerSkillHygieneValidate,
)

__all__ = ["create_handler_skill_hygiene_validate"]


def create_handler_skill_hygiene_validate() -> HandlerSkillHygieneValidate:
    """Build the handler with its default dependencies."""
    return HandlerSkillHygieneValidate()
