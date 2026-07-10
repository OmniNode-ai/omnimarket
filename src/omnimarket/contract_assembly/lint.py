# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure contract-lint gate over an assembled contract document.

Guards the parent serialization output: the assembled YAML must parse, be a
mapping, carry the required top-level sections, and declare operations for every
subcontract. A structurally broken document fails the lint (data, not exception),
so the parent can surface a typed verdict rather than emitting a bad contract.
"""

from __future__ import annotations

import yaml

from omnimarket.contract_assembly.models import EnumLintStatus, ModelLintResult

_REQUIRED_TOP_LEVEL_KEYS = ("metadata", "subcontracts", "advanced_features")


def lint_contract(contract_yaml: str) -> ModelLintResult:
    """Lint an assembled contract document, returning a typed pass/fail verdict."""

    try:
        parsed = yaml.safe_load(contract_yaml)
    except yaml.YAMLError as exc:
        return ModelLintResult(
            status=EnumLintStatus.FAIL,
            messages=(f"yaml parse error: {exc}",),
        )

    if not isinstance(parsed, dict):
        return ModelLintResult(
            status=EnumLintStatus.FAIL,
            messages=("contract root is not a mapping",),
        )

    messages: list[str] = []
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in parsed:
            messages.append(f"missing required top-level key: {key}")

    subcontracts = parsed.get("subcontracts")
    if isinstance(subcontracts, dict):
        for name, body in subcontracts.items():
            if not isinstance(body, dict) or "operations" not in body:
                messages.append(f"subcontract '{name}' declares no operations")
    elif "subcontracts" in parsed:
        messages.append("subcontracts section is not a mapping")

    status = EnumLintStatus.PASS if not messages else EnumLintStatus.FAIL
    return ModelLintResult(status=status, messages=tuple(messages))


__all__ = ["lint_contract"]
