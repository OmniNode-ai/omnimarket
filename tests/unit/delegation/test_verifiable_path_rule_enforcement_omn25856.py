# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25856: a rule that did not veto must not be recorded as a blocking failure.

Gateway workflow 12b6ceb5 (code_generation, function-only answer) ended ``completed``
while its ``rule_evaluations`` held ``follows_codebase_conventions`` passed=false,
enforcement=blocking. On the verifiable-acceptance path (executed tests are the
authority) the heuristic marker rules are reject-only telemetry and never veto, so
the record contradicted the terminal status. A blocking failure must still fail a
run on the non-verifiable path.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FUNCTION_ONLY = "```python\ndef add(a, b):\n    return a + b\n```"
_PROMPT = "Write a function add(a, b) that returns the sum."


def _run(task_type: str, tests_ran: bool) -> ModelQualityGateResult:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, prompt=_PROMPT)
    return quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=_FUNCTION_ONLY,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=_PROMPT,
        named_test_run=(
            ModelNamedTestRun(test_ids=("t::add",), exit_code=0) if tests_ran else None
        ),
    )


def test_known_good_accepted_run_records_no_blocking_failure() -> None:
    result = _run("code_generation", tests_ran=True)
    assert result.passed is True
    failed_blocking = [
        e
        for e in result.rule_evaluations
        if e.enforcement == "blocking" and not e.passed
    ]
    assert failed_blocking == []
    conv = [
        e for e in result.rule_evaluations if e.rule == "follows_codebase_conventions"
    ]
    # The prompt does not ask for conventions, so the rule is waived (not evaluated)
    # rather than recorded as a failure.
    assert conv == []


def test_known_bad_blocking_failure_still_fails_the_run() -> None:
    result = _run("code_generation", tests_ran=False)
    failed_blocking = [
        e
        for e in result.rule_evaluations
        if e.enforcement == "blocking" and not e.passed
    ]
    # Whenever a failed blocking rule is recorded, the run must not have passed.
    assert not (failed_blocking and result.passed)
