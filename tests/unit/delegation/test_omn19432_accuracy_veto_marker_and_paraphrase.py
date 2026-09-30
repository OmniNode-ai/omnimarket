# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19432: the accuracy veto does not refuse the gate's own marker or a restated input fact.

Measured 2026-09-30 in the GLM lineup audit re-run, on glm-5.3 answers the blind
grader marked correct and the delegate gate marked failed on ``accurate``:

* ``fa2afee6``: the ``claims_grounded`` directive tells the model "if a state is
  essential and not given, write (unverified) right after it". The model did, and
  the blocking rule ``accurate`` vetoed the word "unverified". The gate refused the
  answer for following the gate's own instruction.
* ``69ef9cc7``: the input said "whether it carries the same identity defect is
  unverified". The answer restated it as "whether that tenant carries the same
  identity defect remains unverified". The OMN-19433 quote check needs the words
  beside the phrase to match the input verbatim, and "is" against "remains" broke
  the match, so an answer relaying the input's own uncertainty was read as the
  answer disclaiming its own accuracy.

Both are false rejections that spend a paid rung. Neither loosens the rule for a
first-person disclaimer: those tests stay in
``test_omn19433_accuracy_veto_ignores_quoted_input.py`` and are repeated here as
positive controls.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("6f0b1c52-7c5e-4d6a-93a1-2f2f0d3a19b7")
_ACCURACY_VETO = "explicitly disclaims accuracy"

_SOURCE = (
    "The compose dev lane now carries the canonical house tenant. The equivalent "
    "check on the cluster tenant has not been performed, so whether it carries the "
    "same identity defect is unverified. Six PRs were merged by others; PR 11889 is "
    "armed and CI is running."
)


def _reasons(answer: str, source: str | None) -> tuple[str, ...]:
    return delta(
        ModelQualityGateInput(
            correlation_id=_CORRELATION_ID,
            task_type="document",
            llm_response_content=answer,
            dod_deterministic=("response_non_empty",),
            dod_heuristic=("no_refusal", "accurate", "semantic_adequacy"),
        ),
        grounding_source=source,
    ).failure_reasons


def _vetoed(reasons: tuple[str, ...]) -> bool:
    return any(_ACCURACY_VETO in reason for reason in reasons)


def test_the_marker_the_directive_asks_for_is_not_a_disclaimer() -> None:
    answer = (
        "Merged by others: six PRs. PR 11889 is armed and its final result is "
        "(unverified)."
    )
    assert not _vetoed(_reasons(answer, _SOURCE)), _reasons(answer, _SOURCE)
    # The marker is the gate's own vocabulary: it holds with no input to compare to.
    assert not _vetoed(_reasons(answer, None))


def test_a_bare_unverified_that_is_not_the_marker_still_vetoes() -> None:
    answer = "Merged by others: six PRs. The final result is unverified overall."
    assert _vetoed(_reasons(answer, "Nothing about this is stated in the input."))


def test_the_marker_does_not_excuse_a_real_disclaimer_beside_it() -> None:
    answer = "PR 11889 is armed (unverified). I cannot verify that CI passed."
    assert _vetoed(_reasons(answer, _SOURCE))


def test_restating_the_input_uncertainty_with_a_different_copula_is_not_vetoed() -> (
    None
):
    answer = (
        "The house tenant is live on the compose dev lane, so whether that tenant "
        "carries the same identity defect remains unverified."
    )
    assert not _vetoed(_reasons(answer, _SOURCE)), _reasons(answer, _SOURCE)


def test_the_same_restatement_vetoes_when_the_input_never_said_it() -> None:
    """Positive control: the relaxation is tied to the input, not to the phrase."""
    answer = (
        "The house tenant is live on the compose dev lane, so whether that tenant "
        "carries the same identity defect remains unverified."
    )
    assert _vetoed(
        _reasons(answer, "The house tenant is live on the compose dev lane.")
    )
    assert _vetoed(_reasons(answer, None))


@pytest.mark.parametrize(
    "disclaimer",
    [
        "I cannot verify that the migration ran, so treat this as a guess.",
        "I am not sure the count is right.",
        "Overall, this report is unverified and may be inaccurate.",
    ],
)
def test_first_person_disclaimers_still_veto_with_an_input_supplied(
    disclaimer: str,
) -> None:
    assert _vetoed(_reasons(f"PR 11889 is armed. {disclaimer}", _SOURCE))
