# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19401: correct causal answers from lab delegations the check still vetoed.

Replaying 57 reasoning answers of Qwen3.8-27B from lab delegation runs
(2026-10-20) through the step_by_step_explanation check refused three, all of
them correct short explanations. Two walk the mechanism through with a
causative verb ("makes", "cause") or a result clause after a comma
(", helping", ", overwhelming"), which no marker carried. The third ("It can
return stale data when the original changes") explains by a condition alone;
"when" is in too much prose that explains nothing to be a marker, so it is not
in this fixture. The negative controls are answers that assert without
explaining, plus prose that holds a new marker's letters outside the causal
use ("decision making", "because" is not "cause"): those must still fail.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _check_contains_any,
)

pytestmark = pytest.mark.unit

_VETO = "TASK_MISMATCH: failed step_by_step_explanation"

_CORRECT_CAUSAL_ANSWERS: dict[str, str] = {
    # Verbatim Qwen3.8-27B answer, lab delegation run
    # 8f076b00-5b90-41b8-877d-230a5938b4b4 (2026-10-20).
    "lab_interest_rates_makes": (
        "Raising interest rates makes borrowing more expensive, which reduces "
        "spending and investment. Lower demand eases pressure on prices, "
        "helping inflation fall."
    ),
    # Verbatim Qwen3.8-27B answer, lab delegation run
    # eda24477-4e76-4ce5-9e46-a93ce16ee204 (2026-10-20).
    "lab_retry_storm_cause": (
        "A retry storm multiplies load on a service that is already under "
        "stress, overwhelming its capacity to recover. The added requests can "
        "exhaust resources, block recovery, and cause the service to fail again."
    ),
    "causing": (
        "A slow query holds a row lock, causing every other writer to that row "
        "to queue behind it."
    ),
    "leading_to": (
        "Warm water expands as its molecules move apart, leading to a higher "
        "sea level in a basin of fixed size."
    ),
    "can_lead_to": (
        "An unbounded queue keeps accepting work past capacity. That can lead "
        "to memory exhaustion long before anyone sees an error."
    ),
    "can_result_in": (
        "Two clients read the same balance and both write back their own sum. "
        "That can result in one of the two deposits being lost."
    ),
    "resulting_in": (
        "Each retry adds load to a struggling service, resulting in more "
        "timeouts and more retries."
    ),
    "comma_making": (
        "A full disk leaves no room for the write-ahead log, making every "
        "transaction that must log a change fail."
    ),
    "comma_allowing": (
        "A load balancer health-checks each backend and stops routing to a "
        "failed one, allowing the remaining servers to keep serving traffic."
    ),
    "comma_forcing": (
        "A missing index on the foreign key leaves no fast path to child rows, "
        "forcing a full scan of the child table on every delete."
    ),
    "this_is_why": (
        "Python evaluates a default argument once, when the function is "
        "defined. This is why a list used as a default is shared by every call."
    ),
    "thats_why": (
        "Metal conducts heat away from skin much faster than wood. That's why "
        "it feels colder at the same temperature."
    ),
    "thereby": (
        "Salt dissolves into the thin film of water on ice and lowers its "
        "freezing point, thereby melting the ice at road temperatures."
    ),
}

_NON_EXPLANATIONS: dict[str, str] = {
    "bare_assertion": (
        "Hash map lookups are constant time on average and linear in the worst case."
    ),
    "list_of_facts": "TCP is reliable. UDP is fast. Both are transport protocols.",
    "decision_making": "Decision making is hard for large teams.",
    "making_inside_a_word": "Policymaking and lawmaking are separate branches.",
    "allowing_without_comma": "The setting is worth allowing for small teams.",
    "refusal": "I cannot answer that question.",
}


def _run_step_by_step_check(content: str) -> str | None:
    category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    return _check_contains_any(
        content,
        check_name="step_by_step_explanation",
        category=category,
        markers=markers,
    )


@pytest.mark.parametrize(
    "content",
    list(_CORRECT_CAUSAL_ANSWERS.values()),
    ids=list(_CORRECT_CAUSAL_ANSWERS),
)
def test_correct_causal_answer_passes_step_by_step_explanation(content: str) -> None:
    assert _run_step_by_step_check(content) is None


@pytest.mark.parametrize(
    "content",
    list(_NON_EXPLANATIONS.values()),
    ids=list(_NON_EXPLANATIONS),
)
def test_non_explanation_is_still_vetoed(content: str) -> None:
    assert _run_step_by_step_check(content) == _VETO
