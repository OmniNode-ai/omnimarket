# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed deterministic acceptance for pinned single-attempt delegation."""

from __future__ import annotations

import json
from uuid import UUID

import jsonschema

from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult


def evaluate_pinned_response_contract(
    *,
    correlation_id: UUID,
    content: str,
    response_contract: dict[str, object] | None,
) -> ModelQualityGateResult:
    """Validate only an explicitly supplied response schema, with no fallback.

    A selected pinned policy has one closed acceptance authority.  In particular,
    it does not inherit task-class DoD, legacy keyword checks, or any judge
    availability behaviour.  Absence of the caller's schema is a terminal
    deterministic rejection, rather than permission to synthesize one.
    """
    if response_contract is None:
        return ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=("pinned_response_contract_required",),
            fallback_recommended=False,
        )
    if not content.strip():
        return ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=(
                "MALFORMED: empty response fails pinned response contract",
            ),
            fallback_recommended=False,
        )
    try:
        candidate = json.loads(content)
    except json.JSONDecodeError as exc:
        return ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=(f"MALFORMED: response is not valid JSON: {exc.msg}",),
            fallback_recommended=False,
        )
    validator_cls = jsonschema.validators.validator_for(response_contract)
    validator_cls.check_schema(response_contract)
    errors = sorted(
        validator_cls(response_contract).iter_errors(candidate),
        key=lambda error: [str(part) for part in error.path],
    )
    if errors:
        return ModelQualityGateResult(
            correlation_id=correlation_id,
            passed=False,
            fail_category="fail_deterministic",
            quality_score=0.0,
            failure_reasons=tuple(
                "SCHEMA_VIOLATION: "
                f"{'.'.join(str(part) for part in error.path) or '<root>'}: "
                f"{error.message}"
                for error in errors
            ),
            fallback_recommended=False,
        )
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=True,
        fail_category="pass",
        quality_score=1.0,
        failure_reasons=(),
        fallback_recommended=False,
    )


__all__ = ["evaluate_pinned_response_contract"]
