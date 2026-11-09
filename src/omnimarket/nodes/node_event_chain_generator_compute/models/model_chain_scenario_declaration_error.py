# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed error the scenario test generator raises for a declaration it refuses (OMN-22892)."""

from __future__ import annotations

from omnibase_core.enums.enum_core_error_code import EnumCoreErrorCode
from omnibase_core.errors.model_onex_error import ModelOnexError


class ChainScenarioDeclarationError(ModelOnexError):
    """A chain scenario declaration cannot yield a test that fails.

    ``rule_ids`` are the omnibase_core chain scenario validator rules the declaration
    broke (for example ``case_without_named_breaks`` or ``generated_test_cannot_fail``),
    so a caller can branch on the rule and the same declaration makes the validator
    CLI exit 1.
    """

    def __init__(
        self,
        node_name: str,
        rule_ids: tuple[str, ...],
        issues: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"chain_scenarios of {node_name} cannot yield a test: {'; '.join(issues)}",
            error_code=EnumCoreErrorCode.VALIDATION_ERROR,
        )
        self.node_name = node_name
        self.rule_ids = rule_ids
        self.issues = issues


__all__ = ["ChainScenarioDeclarationError"]
