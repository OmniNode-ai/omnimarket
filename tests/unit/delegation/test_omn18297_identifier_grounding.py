# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Identifier grounding in the delegation quality gate (OMN-18297).

RED-first anchor: the recorded output of local delegation
``d715f096-27b9-444f-9355-3554819ef8a5`` replayed against its own recorded
input. Before this ticket the gate scored that response ``passed=true
score=1.0``; it cites eight pull requests that occur nowhere in the 22 ledger
rows it was given.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.delegation.identifier_grounding import (
    answer_segment,
    evaluate_identifier_grounding,
    resolve_identifier_grounding_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "omn18297"
_SOURCE = _FIXTURES / "d715f096_grounding_source.txt"
_RESPONSE = _FIXTURES / "d715f096_response_answer_segment.txt"

# Verified absent from the recorded source in ANY form -- neither the
# repository-qualified spelling nor the bare number token appears.
_RECORDED_UNGROUNDED: frozenset[str] = frozenset(
    {"#9273", "#9282", "#9292", "#9300", "#9303", "#9306", "#9308", "#388"}
)

_DOCUMENT_DOD_DETERMINISTIC: tuple[str, ...] = ("response_non_empty",)
_DOCUMENT_DOD_HEURISTIC: tuple[str, ...] = (
    "no_refusal",
    "accurate",
    "semantic_adequacy",
    "identifiers_grounded",
)


@pytest.fixture(scope="module")
def recorded_source() -> str:
    return _SOURCE.read_text()


@pytest.fixture(scope="module")
def recorded_response() -> str:
    return _RESPONSE.read_text()


def _gate_input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content=content,
        dod_deterministic=_DOCUMENT_DOD_DETERMINISTIC,
        dod_heuristic=_DOCUMENT_DOD_HEURISTIC,
    )


def test_recorded_response_has_ungrounded_pull_request_citations(
    recorded_source: str, recorded_response: str
) -> None:
    """The recorded pair is the RED anchor: eight ungrounded citations."""
    verdict = evaluate_identifier_grounding(
        content=recorded_response,
        grounding_source=recorded_source,
        policy=resolve_identifier_grounding_policy(),
    )

    assert verdict.evaluated is True
    assert verdict.checked_count > 0
    looked_up = {item.looked_up_as for item in verdict.ungrounded}
    assert looked_up == _RECORDED_UNGROUNDED
    assert {item.class_name for item in verdict.ungrounded} == {"pull_request_ref"}


def test_every_ticket_identifier_in_the_recorded_response_is_grounded(
    recorded_source: str, recorded_response: str
) -> None:
    """The finding's other half: ticket numbers were all correct."""
    verdict = evaluate_identifier_grounding(
        content=recorded_response,
        grounding_source=recorded_source,
        policy=resolve_identifier_grounding_policy(),
    )

    assert [
        item for item in verdict.ungrounded if item.class_name == "ticket_ref"
    ] == []


def test_gate_fails_the_recorded_response_and_names_the_ungrounded_identifiers(
    recorded_source: str, recorded_response: str
) -> None:
    """RED before OMN-18297: this exact response scored passed=true score=1.0."""
    result = delta(_gate_input(recorded_response), grounding_source=recorded_source)

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    assert result.quality_score < 1.0
    # An ungrounded response is recoverable by a stronger model, so it escalates.
    assert result.fallback_recommended is True
    assert any(reason.startswith("UNGROUNDED") for reason in result.failure_reasons), (
        result.failure_reasons
    )
    reported = {entry.split(":", 1)[1] for entry in result.ungrounded_identifiers}
    assert {"onex_change_control#9273", "onex_change_control#9306"} <= reported
    assert len(result.ungrounded_identifiers) == len(_RECORDED_UNGROUNDED)


def test_gate_passes_a_fully_grounded_response_of_the_same_shape(
    recorded_source: str,
) -> None:
    """GREEN counterpart: same shape, every identifier present in the source."""
    grounded = (
        "**Friction Lane**\n"
        "- Tickets: OMN-18247, OMN-18253, OMN-18249\n"
        "- PRs: omnibase_infra#3466, #3470, #3473\n"
        "- Summary: Landed the grader repair and the CI evidence policy. Each "
        "cited pull request and ticket is drawn from the fed rows, because "
        "every identifier in this paragraph occurs verbatim in the source "
        "text it was derived from, and nothing was invented to fill a gap.\n"
    )

    result = delta(_gate_input(grounded), grounding_source=recorded_source)

    assert result.ungrounded_identifiers == ()
    assert not any(
        reason.startswith("UNGROUNDED") for reason in result.failure_reasons
    ), result.failure_reasons
    assert result.passed is True


def test_an_identifier_marked_unverified_is_not_reported(recorded_source: str) -> None:
    """The contract lets a response cite what it could not verify, if it says so."""
    marked = (
        "- PRs: omnibase_infra#9999 (unverified), omnibase_infra#3466\n"
        "- Summary: One citation could not be resolved against the fed rows and "
        "is marked as such, which is the declared alternative to omitting it "
        "entirely from this rollup of the window's landed work.\n"
    )

    result = delta(_gate_input(marked), grounding_source=recorded_source)

    assert result.ungrounded_identifiers == ()


def test_no_grounding_source_records_a_skipped_check_never_a_pass() -> None:
    """The bus path carries no prompt; an unevaluated check must not pass."""
    ungrounded_text = (
        "- PRs: omnibase_infra#9999, onex_change_control#8888\n"
        "- Summary: This rollup cites two pull requests and is being evaluated "
        "with no grounding source available at all, which is the bus path's "
        "situation today and must never read as a clean grounding result.\n"
    )

    result = delta(_gate_input(ungrounded_text), grounding_source=None)

    assert "identifiers_grounded" in result.skipped_checks
    assert result.ungrounded_identifiers == ()
    assert not any(reason.startswith("UNGROUNDED") for reason in result.failure_reasons)


def test_skipped_grounding_check_is_excluded_from_the_scored_total(
    recorded_source: str,
) -> None:
    """An unevaluated check must not contribute a phantom pass to the score."""
    text = (
        "- PRs: omnibase_infra#3466\n"
        "- Summary: A short but complete rollup paragraph whose only citation "
        "is grounded in the fed rows, used here to compare the scored total "
        "with and without an available grounding source.\n"
    )

    with_source = delta(_gate_input(text), grounding_source=recorded_source)
    without_source = delta(_gate_input(text), grounding_source=None)

    assert with_source.quality_score == without_source.quality_score


def test_answer_segment_reads_only_after_the_last_stray_terminator() -> None:
    """OMN-18278's leak is isolated for this check, not repaired here."""
    policy = resolve_identifier_grounding_policy()
    terminator = policy.answer_segment.stray_trace_terminator
    leaked = f"scratch: consider omnibase_infra#9999 {terminator} answer: #3466"

    assert answer_segment(leaked, terminator=terminator).strip() == "answer: #3466"


def test_grounding_check_is_reject_only_and_never_adequacy_authority() -> None:
    """A response that invents nothing is not thereby an adequate answer."""
    from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
        _has_adequacy_authority,
    )

    assert _has_adequacy_authority((), ("no_refusal", "identifiers_grounded")) is False


def test_contract_declares_the_policy_rather_than_the_handler() -> None:
    """The classes and the failure policy are contract facts."""
    policy = resolve_identifier_grounding_policy()

    assert policy.policy_version == "identifier-grounding.v1"
    assert policy.check_name == "identifiers_grounded"
    assert {c.class_name for c in policy.classes} == {
        "pull_request_ref",
        "ticket_ref",
        "commit_sha",
        "github_run_id",
        "correlation_uuid",
    }
    assert policy.failure_policy.on_ungrounded == "fail"
    assert policy.failure_policy.on_source_unavailable == "skip_and_record"


def test_every_prose_task_class_arms_the_grounding_check() -> None:
    """Wiring proof: declaring the check is what arms it."""
    from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
        resolve_task_class_dod_checks,
    )

    prose_classes = (
        "document",
        "documentation",
        "summarization",
        "research",
        "review",
        "code_review",
        "reasoning",
        "complex_reasoning",
        "planning",
    )
    for task_class in prose_classes:
        _, heuristic = resolve_task_class_dod_checks(task_class)
        assert "identifiers_grounded" in heuristic, task_class


def test_pre_omn18297_band_reproduces_the_recorded_passed_true_score_one(
    recorded_response: str, recorded_source: str
) -> None:
    """The defect, pinned: the old band scores this exact response 1.0.

    ``document``'s heuristic band before this ticket was
    ``(no_refusal, accurate, semantic_adequacy)``. Evaluated with that band the
    recorded response -- the one carrying eight invented pull-request citations
    -- passes with a perfect score, which is what the live gate reported on
    2026-09-13. Declaring ``identifiers_grounded`` is the whole of the fix, so
    this test fails the day someone removes the check from the band and
    believes the class is still covered.
    """
    pre_fix = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="document",
        llm_response_content=recorded_response,
        dod_deterministic=_DOCUMENT_DOD_DETERMINISTIC,
        dod_heuristic=("no_refusal", "accurate", "semantic_adequacy"),
    )

    recorded = delta(pre_fix, grounding_source=recorded_source)

    assert recorded.passed is True
    assert recorded.quality_score == 1.0
    assert recorded.ungrounded_identifiers == ()
