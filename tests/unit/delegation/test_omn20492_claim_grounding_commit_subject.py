# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""A commit subject's imperative verb is not an asserted state (OMN-20492).

RED-first anchor: experiment E4 (OMN-20422), three delegated landing commit
messages (leases 2191, 2200, 2237) drafted for ``summarization``. Each is
``fix(OMN-17427): resolve ...``. ``claims_grounded`` read ``resolve`` in the
subject as the ``completed`` state asserted about OMN-17427, found no word of
that group on the source rows holding OMN-17427, and vetoed a faithful draft on
every rung of the route ladder.

The fixtures are the recorded prompt (the grounding source) and the recorded
draft of each lease. The negative controls prove the exemption is the imperative
verb of a subject only: a state asserted about a real identifier still vetoes.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.content_grounding import (
    evaluate_claim_grounding,
    resolve_claim_grounding_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "omn20492"
_LEASES = ("2191", "2200", "2237")


def _fixture(lease: str, kind: str) -> str:
    return (_FIXTURES / f"lease{lease}_{kind}.txt").read_text()


def _gate_input(content: str, source: str) -> ModelQualityGateInput:
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", source)
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="summarization",
        llm_response_content=content,
        dod_deterministic=deterministic,
        dod_heuristic=heuristic,
    )


@pytest.mark.parametrize("lease", _LEASES)
def test_commit_subject_with_imperative_resolve_is_not_a_completed_claim(
    lease: str,
) -> None:
    verdict = evaluate_claim_grounding(
        content=_fixture(lease, "draft"),
        grounding_source=_fixture(lease, "source"),
        policy=resolve_claim_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert verdict.ungrounded == (), [u.clause for u in verdict.ungrounded]


@pytest.mark.parametrize("lease", _LEASES)
def test_gate_does_not_veto_the_recorded_draft_on_claims_grounded(lease: str) -> None:
    source = _fixture(lease, "source")
    result = delta(
        _gate_input(_fixture(lease, "draft"), source), grounding_source=source
    )

    assert not any(
        reason.startswith("UNGROUNDED:") and "claim" in reason
        for reason in result.failure_reasons
    ), result.failure_reasons
    assert result.ungrounded_identifiers == ()
    assert any(
        evaluation.rule == "claims_grounded" and evaluation.passed
        for evaluation in result.rule_evaluations
    )


_SOURCE = (
    "Ticket: OMN-17427\n"
    "Pull request: OmniNode-ai/omnimarket#3323\n"
    "What the worker reported: kept both sides of the conflict.\n"
)


def test_negative_control_an_ungrounded_state_about_a_real_identifier_still_vetoes() -> (
    None
):
    content = "fix(OMN-17427): bump the pins\n\nOMN-17427 is deprioritized.\n"
    verdict = evaluate_claim_grounding(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_claim_grounding_policy(),
    )

    assert [u.group for u in verdict.ungrounded] == ["deprioritized"]
    assert verdict.ungrounded[0].anchor == "OMN-17427"


def test_negative_control_a_state_later_in_the_subject_still_vetoes() -> None:
    content = "fix(OMN-17427): resolve the conflict, OMN-17427 is blocked\n"
    verdict = evaluate_claim_grounding(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_claim_grounding_policy(),
    )

    assert [u.group for u in verdict.ungrounded] == ["blocked"]


def test_negative_control_resolved_in_prose_about_an_identifier_still_vetoes() -> None:
    content = "The ticket OMN-17427 was resolved last week.\n"
    verdict = evaluate_claim_grounding(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_claim_grounding_policy(),
    )

    assert [u.group for u in verdict.ungrounded] == ["completed"]


def test_negative_control_a_subject_verb_that_is_not_the_first_word_still_vetoes() -> (
    None
):
    content = "fix(OMN-17427): the work is now merged\n"
    verdict = evaluate_claim_grounding(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_claim_grounding_policy(),
    )

    assert [u.group for u in verdict.ungrounded] == ["completed"]


def test_a_ticket_id_inside_a_file_path_is_not_an_anchor() -> None:
    content = "Resolved the conflict in contracts/OMN-17427.yaml.\n"
    verdict = evaluate_claim_grounding(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_claim_grounding_policy(),
    )

    assert verdict.ungrounded == ()


def test_negative_control_past_tense_in_a_subject_still_vetoes() -> None:
    content = "fix(OMN-17427): resolved the conflict\n"
    verdict = evaluate_claim_grounding(
        content=content,
        grounding_source=_SOURCE,
        policy=resolve_claim_grounding_policy(),
    )

    assert [u.group for u in verdict.ungrounded] == ["completed"]
