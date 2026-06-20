# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13375: reject-only-only deterministic DoD cannot promote to passed.

OMN-13373 (#1302) made ``no_refusal`` a reject-only deterministic pre-filter so
``_has_adequacy_authority`` excludes it. A deeper promotion leak survived: the
``deterministic_acceptance_authority`` branch in ``delta`` promoted a clean
output to ``passed=True`` whenever ``task_type`` was a verifiable type
(``code_generation``/``test``/``validator_generation``) and ``dod_deterministic``
was non-empty — without requiring any NON-reject-only deterministic check. A
request with ``acceptance_criteria=["no_refusal"]`` (merged into the
deterministic band) plus a clean, non-refusing output therefore leaked to
``passed=True`` on a verifiable task type, even though ``no_refusal`` alone can
only REJECT a refusal — never grant adequacy (OMN-13370).

This module proves the invariant in both directions:

* a ``dod_deterministic`` set whose checks are ALL reject-only keeps its reject
  power but cannot promote a clean output to adequate — it falls through to the
  ``_has_adequacy_authority`` guard and yields ``passed=False`` with
  ``fallback_recommended=True`` (OMN-13370 / OMN-13373 reject behavior intact),
* a control with at least one real verifiable (non-reject-only) deterministic
  check still grants ``passed=True`` on the same clean output, proving the guard
  is scoped to the reject-only-only case.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

# Clean, non-refusing, well-formed Python output. It parses and compiles, so a
# real verifiable deterministic check (the control) passes on it — isolating the
# variable to "reject-only-only band" vs "has a promotion-capable check".
_CLEAN_CODE_OUTPUT = "def add(a: int, b: int) -> int:\n    return a + b\n"

# The three verifiable task types that gate deterministic acceptance authority.
_VERIFIABLE_TASK_TYPES = ("code_generation", "test", "validator_generation")

# The OMN-13373 reject-only adequacy-authority failure reason emitted by the
# _has_adequacy_authority guard when no promotion-capable check is present.
_NO_ADEQUACY_AUTHORITY_REASON = (
    "TASK_MISMATCH: no deterministic acceptance or judge adequacy authority; "
    "schema/length/no-refusal/marker checks are reject-only"
)


def _gate(
    content: str,
    *,
    task_type: str,
    deterministic: tuple[str, ...],
) -> ModelQualityGateResult:
    """Drive the gate via the deterministic band the way a request does."""
    return quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=deterministic,
        )
    )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _VERIFIABLE_TASK_TYPES)
def test_reject_only_only_deterministic_band_does_not_promote_clean_output(
    task_type: str,
) -> None:
    """A clean output is NOT promoted when the band is all reject-only checks.

    This is the OMN-13375 promotion leak: ``no_refusal`` is the only declared
    deterministic check, the output is clean (no refusal), so the band produces
    zero deterministic failures. Before the fix the
    ``deterministic_acceptance_authority`` branch promoted this to
    ``passed=True`` on a verifiable task type. The fix requires at least one
    NON-reject-only deterministic check before that branch can grant adequacy.
    """
    result = _gate(
        _CLEAN_CODE_OUTPUT,
        task_type=task_type,
        deterministic=("no_refusal",),
    )

    assert result.passed is False, result
    assert result.fallback_recommended is True, result
    assert result.fail_category == "fail_heuristic", result
    assert result.failure_reasons == (_NO_ADEQUACY_AUTHORITY_REASON,), result


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _VERIFIABLE_TASK_TYPES)
def test_reject_only_only_deterministic_band_still_rejects_refusal(
    task_type: str,
) -> None:
    """The reject-only band keeps its reject power (OMN-13373 preserved).

    A refusal in the deterministic band must still hard-fail with a
    ``fail_deterministic`` verdict and an escalation-worthy ``REFUSAL:`` detail —
    closing the promotion leak does not weaken rejection.
    """
    result = _gate(
        "I cannot help with that request.",
        task_type=task_type,
        deterministic=("no_refusal",),
    )

    assert result.passed is False, result
    assert result.fail_category == "fail_deterministic", result
    assert result.fallback_recommended is True, result
    assert any(reason.startswith("REFUSAL") for reason in result.failure_reasons), (
        result.failure_reasons
    )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _VERIFIABLE_TASK_TYPES)
def test_real_verifiable_deterministic_check_still_promotes_clean_output(
    task_type: str,
) -> None:
    """Control: a non-reject-only deterministic check still grants passed=True.

    ``compiles_without_errors`` is a real verifiable (promotion-capable)
    deterministic check. On the same clean, compiling output the gate must still
    return ``passed=True`` — proving the OMN-13375 guard is scoped to the
    reject-only-only band and does not regress legitimate deterministic
    acceptance.
    """
    result = _gate(
        _CLEAN_CODE_OUTPUT,
        task_type=task_type,
        deterministic=("compiles_without_errors",),
    )

    assert result.passed is True, result
    assert result.fail_category == "pass", result
    assert result.fallback_recommended is False, result
    assert result.failure_reasons == (), result
