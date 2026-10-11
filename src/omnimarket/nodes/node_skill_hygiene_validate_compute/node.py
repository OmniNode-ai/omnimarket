# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute node for skill-hygiene validation; the contract routes to the handler."""

from __future__ import annotations

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_skill_hygiene_validate_compute.models import (
    ModelSkillHygieneValidateRequest,
    ModelSkillHygieneValidateResult,
)


class NodeSkillHygieneValidateCompute(
    NodeCompute[ModelSkillHygieneValidateRequest, ModelSkillHygieneValidateResult]
):
    """Thin shell: HandlerSkillHygieneValidate, bound in contract.yaml, owns the logic."""


__all__ = ["NodeSkillHygieneValidateCompute"]
