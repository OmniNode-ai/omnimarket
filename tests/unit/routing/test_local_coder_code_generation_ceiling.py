# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""local-coder's code_generation budget is its measured correct ceiling (OMN-22220).

The task-complexity-acceptance eval measured local-coder correct up to 8169
input tokens (R6-23, scorer 1.0). The code_generation budget was 32000, so a
24579-token task (H6-05) was routed to local-coder first and its answer was
gate-passed at quality_score 0.733 while the scorer gave it 0.0. The ceiling is
declared in ``configs/grounding_budget_by_task_class.yaml``; routing skips the
rung above it, and an above-ceiling answer the scorer did not pass is refused.

Every fixture is in memory: the tier ladder comes from the packaged routing
contract and the backends are fakes, so no endpoint is called.
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

import pytest

from omnimarket.handlers.routing.measured_correct_ceiling import (
    load_grounding_budget_by_task_class,
    refuse_above_measured_correct_ceiling,
)
from omnimarket.models.delegation.wire import ModelDelegationConfig, ModelTierModel
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from tests.placed_routing_config import placed_routing_config

pytestmark = pytest.mark.unit

_Outcome = Literal["PASS", "FAIL", "UNDETERMINED"]
_CODER = "local-coder"
_TASK = "code_generation"
_CEILING = 8169  # R6-23, the largest input local-coder answered correctly.

# Scrubbed recorded run results from the task-complexity-acceptance eval: the
# item id, its measured input tokens, the gate's verdict and score, and the
# scorer's score. Prompt and answer text are not recorded here.
_RECEIPTS: dict[str, dict[str, float | int | bool]] = {
    "H6-05": {
        "input_tokens": 24579,
        "gate_passed": True,
        "quality_score": 0.733,
        "scorer": 0.0,
    },
    "H6-04": {
        "input_tokens": 4560,
        "gate_passed": True,
        "quality_score": 0.733,
        "scorer": 0.0,
    },
}


def _prompt_of(tokens: int) -> str:
    """A prompt that measures ``tokens`` in the routing estimator's units."""
    return "x" * (tokens * 4)


def _fake_backends(
    config: ModelDelegationConfig,
) -> dict[str, routing.BifrostBackendRef]:
    refs = {model.backend_ref for tier in config.tiers for model in tier.models}
    return {
        ref: routing.BifrostBackendRef(
            endpoint_url=f"http://{ref}.invalid/v1",
            model_name=f"model-{ref}",
            timeout_ms=1000,
            max_tokens=256,
        )
        for ref in refs
    }


def _first_route(tokens: int) -> tuple[str, str] | None:
    """The tier and backend the ladder offers a code_generation task first."""
    config = placed_routing_config()
    backends = _fake_backends(config)
    for tier in config.tiers:
        selected: ModelTierModel | None = routing._select_model_for_task(
            tier.models,
            _TASK,
            tokens,
            backends,
            require_credential=False,
        )
        if selected is not None:
            return tier.name, selected.backend_ref
    return None


def _gate_passed(score: float) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=uuid4(), passed=True, quality_score=score
    )


def _verdict(
    outcome: _Outcome, **criteria: tuple[str, ...]
) -> ModelAttemptRubricVerdict:
    return ModelAttemptRubricVerdict(
        rubric_version="v-test",
        task_class=_TASK,
        outcome=outcome,
        **criteria,
    )


def test_ceiling_and_budget_are_declared_in_data() -> None:
    config = load_grounding_budget_by_task_class()
    assert config.ceiling_for(_CODER, _TASK) == _CEILING
    assert config.budget_for(_CODER, _TASK) == _CEILING
    # Only the named backend and class carry the ceiling.
    assert config.ceiling_for(_CODER, "research") is None
    assert config.ceiling_for("local-heavy-reasoning", _TASK) is None


def test_above_ceiling_not_local_for_a_24579_token_task() -> None:
    route = _first_route(24579)
    assert route is not None
    assert route[1] != _CODER, route
    assert route[0] != "local", route


def test_at_ceiling_still_local_positive_control_above_ceiling_not_local() -> None:
    assert _first_route(_CEILING) == ("local", _CODER)
    assert _first_route(1958) == ("local", _CODER)


def test_undetermined_above_ceiling_answer_is_not_gate_passed() -> None:
    decided = refuse_above_measured_correct_ceiling(
        _gate_passed(0.733),
        _verdict("UNDETERMINED", undetermined_criteria=("passes_existing_tests",)),
        backend_ref=_CODER,
        task_class=_TASK,
        request_text=_prompt_of(24579),
    )
    assert decided.passed is False
    assert decided.fail_category == "rubric_failed"
    assert decided.fallback_recommended is True
    assert any("8169" in reason for reason in decided.failure_reasons)
    assert "rubric_failed: passes_existing_tests" in decided.failure_reasons


@pytest.mark.parametrize(
    ("backend_ref", "tokens", "outcome"),
    [
        (_CODER, 4560, "UNDETERMINED"),  # below the ceiling
        (_CODER, 24579, "PASS"),  # the scorer passed it
        ("cloud-gemini-2-5-flash", 24579, "UNDETERMINED"),  # no ceiling declared
        (None, 24579, "UNDETERMINED"),  # backend not known
    ],
)
def test_undetermined_above_ceiling_leaves_other_answers_unchanged(
    backend_ref: str | None, tokens: int, outcome: _Outcome
) -> None:
    accepted = _gate_passed(0.733)
    decided = refuse_above_measured_correct_ceiling(
        accepted,
        _verdict(outcome, undetermined_criteria=("passes_existing_tests",)),
        backend_ref=backend_ref,
        task_class=_TASK,
        request_text=_prompt_of(tokens),
    )
    assert decided is accepted


def test_receipt_h6_05_routes_off_local_coder_and_is_refused() -> None:
    h6_05 = _RECEIPTS["H6-05"]
    tokens = int(h6_05["input_tokens"])
    assert h6_05["gate_passed"] is True
    assert h6_05["scorer"] == 0.0

    route = _first_route(tokens)
    assert route is not None
    assert route[1] != _CODER, route

    # Had it run on local-coder anyway (a pinned run), the recorded pass stands no more.
    decided = refuse_above_measured_correct_ceiling(
        _gate_passed(float(h6_05["quality_score"])),
        _verdict("FAIL", failed_criteria=("passes_existing_tests",)),
        backend_ref=_CODER,
        task_class=_TASK,
        request_text=_prompt_of(tokens),
    )
    assert decided.passed is False


def test_receipt_h6_04_below_the_ceiling_still_routes_to_local_coder() -> None:
    """H6-04 failed below the ceiling: the ceiling does not move it (positive control)."""
    tokens = int(_RECEIPTS["H6-04"]["input_tokens"])
    assert tokens <= _CEILING
    assert _first_route(tokens) == ("local", _CODER)
