# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute node for skill-contract validation; the contract routes to the handler."""

from __future__ import annotations

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_skill_contract_validate_compute.models import (
    ModelSkillContractValidateRequest,
    ModelSkillContractValidateResult,
)


class NodeSkillContractValidateCompute(
    NodeCompute[ModelSkillContractValidateRequest, ModelSkillContractValidateResult]
):
    """Thin shell: HandlerSkillContractValidate, bound in contract.yaml, owns the logic."""


__all__ = ["NodeSkillContractValidateCompute"]
