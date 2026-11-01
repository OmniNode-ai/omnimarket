# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A drafted reply may not grant approval the source never granted.

Two drafts of one reply wrote an approval in the requester's reviewer's voice
from a source that held no such grant. The acceptance gate scored both 1.0: the
draft was fluent, concise and cited nothing that was not in the source, and no
check read what the draft *decided*.

The check is ``operator_voice``. It is armed per task class by the contract
(the ``definition_of_done.heuristic`` list of ``task_class_contracts.v1.yaml``),
it is a pure function of the draft and the source, and it calls no model.

The recorded text of the two original drafts is not in the repository, so the
two invented-approval fixtures reproduce the described shape: a first-person
approval or go-ahead drafted from a source that holds no such grant. The
fixtures use synthetic text only.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import ModelQualityGateIntent

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.operator_voice_check import (
    evaluate_operator_voice,
    resolve_operator_voice_policy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_operator_voice import (
    ModelOperatorVoiceVerdict,
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

_FIXTURES = Path(__file__).parents[2] / "fixtures" / "delegation" / "operator_voice"
_CONTRACTS = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)
_CHECK = "operator_voice"

# The classes that draft replies. The contract is the authority; this list is
# the test's own statement of what the contract must declare.
_REPLY_CLASSES = ("document", "summarization", "reasoning", "planning")


def _text(name: str) -> str:
    return (_FIXTURES / name).read_text()


def _gate(task_type: str, draft: str, source: str) -> ModelQualityGateResult:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type, source)
    return delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=draft,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        ),
        grounding_source=source,
    )


def _operator_voice_reasons(result: ModelQualityGateResult) -> list[str]:
    return [r for r in result.failure_reasons if r.startswith("OPERATOR_VOICE")]


# -- the four fixtures: the failing tests first --------------------------------


@pytest.mark.parametrize(
    ("draft_file", "phrase"),
    [
        ("draft_invented_approval_a.txt", "I approve"),
        ("draft_invented_approval_b.txt", "approved"),
    ],
)
def test_operator_voice_refuses_an_invented_approval(
    draft_file: str, phrase: str
) -> None:
    result = _gate("summarization", _text(draft_file), _text("source_no_grant.txt"))

    assert result.passed is False
    reasons = _operator_voice_reasons(result)
    assert reasons, result.failure_reasons
    assert phrase in reasons[0], reasons
    assert any(
        evaluation.rule == _CHECK and not evaluation.passed
        for evaluation in result.rule_evaluations
    ), result.rule_evaluations


def test_operator_voice_accepts_a_draft_that_reports_a_grant_the_source_holds() -> None:
    result = _gate(
        "summarization",
        _text("draft_reports_grant.txt"),
        _text("source_with_grant.txt"),
    )

    assert _operator_voice_reasons(result) == [], result.failure_reasons
    assert any(
        evaluation.rule == _CHECK and evaluation.passed
        for evaluation in result.rule_evaluations
    ), result.rule_evaluations


def test_operator_voice_accepts_a_draft_that_quotes_a_refusal_to_approve() -> None:
    result = _gate(
        "summarization",
        _text("draft_quotes_refusal.txt"),
        _text("source_with_refusal.txt"),
    )

    assert _operator_voice_reasons(result) == [], result.failure_reasons
    assert any(
        evaluation.rule == _CHECK and evaluation.passed
        for evaluation in result.rule_evaluations
    ), result.rule_evaluations


def test_operator_voice_accepts_a_plain_summary_of_the_same_source() -> None:
    result = _gate(
        "summarization", _text("draft_plain_summary.txt"), _text("source_no_grant.txt")
    )

    assert _operator_voice_reasons(result) == [], result.failure_reasons
    assert result.passed is True, result.failure_reasons


# -- the pure function: typed result, named check, offending phrase -------------


def test_operator_voice_finding_names_the_check_and_the_offending_phrase() -> None:
    verdict = evaluate_operator_voice(
        draft=_text("draft_invented_approval_a.txt"),
        source=_text("source_no_grant.txt"),
        policy=resolve_operator_voice_policy(),
    )

    assert isinstance(verdict, ModelOperatorVoiceVerdict)
    assert verdict.evaluated is True
    assert [finding.check for finding in verdict.findings] == [_CHECK] * len(
        verdict.findings
    )
    phrases = {finding.phrase.lower() for finding in verdict.findings}
    assert "i approve" in phrases
    assert any(phrase.startswith("go ahead") for phrase in phrases), phrases


@pytest.mark.parametrize(
    "draft",
    [
        "I approve the plan.",
        "Go ahead.",
        "This is approved.",
        "Please proceed with the cutover.",
        "You may proceed.",
        "We consent to the change.",
        "I authorize the rollout.",
    ],
)
def test_operator_voice_refuses_each_first_person_grant_form_without_a_source_grant(
    draft: str,
) -> None:
    verdict = evaluate_operator_voice(
        draft=draft,
        source="The reviewer has not replied yet.",
        policy=resolve_operator_voice_policy(),
    )

    assert verdict.findings, draft


@pytest.mark.parametrize(
    "draft",
    [
        "The request was not approved.",
        "No one has approved it, and nobody said to go ahead.",
        "Approval is still pending.",
        "The plan says to proceed to step three after the checklist.",
        'Quoting the thread: "I cannot approve this yet."',
    ],
)
def test_operator_voice_leaves_negated_pending_and_procedural_text_alone(
    draft: str,
) -> None:
    verdict = evaluate_operator_voice(
        draft=draft,
        source="The reviewer has not replied yet.",
        policy=resolve_operator_voice_policy(),
    )

    assert verdict.findings == (), draft


def test_operator_voice_is_unevaluated_without_a_source() -> None:
    verdict = evaluate_operator_voice(
        draft="I approve the plan.", source=None, policy=resolve_operator_voice_policy()
    )

    assert verdict.evaluated is False
    result = delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content="I approve the plan.",
            dod_deterministic=(),
            dod_heuristic=(_CHECK,),
        )
    )
    assert _CHECK in result.skipped_checks
    assert all(e.rule != _CHECK for e in result.rule_evaluations)


# -- the veto marker: declared in the contract, read by the handler -------------


def _declared_classes_with_check() -> set[str]:
    contract = yaml.safe_load(_CONTRACTS.read_text())
    return {
        name
        for name, entry in contract["task_classes"].items()
        if isinstance(entry, dict)
        and _CHECK in (entry.get("definition_of_done", {}) or {}).get("heuristic", [])
    }


def test_operator_voice_marker_is_declared_on_exactly_the_reply_classes() -> None:
    assert _declared_classes_with_check() == set(_REPLY_CLASSES)


def test_operator_voice_marker_check_name_comes_from_the_gate_contract() -> None:
    assert resolve_operator_voice_policy().check_name == _CHECK


def test_operator_voice_marker_is_a_blocking_rule_with_a_model_directive() -> None:
    contract = yaml.safe_load(_CONTRACTS.read_text())
    rule = contract["quality_rules"][_CHECK]

    assert rule["enforcement"] == "blocking"
    assert rule["model_directive"].strip()


@pytest.mark.parametrize("task_type", _REPLY_CLASSES)
def test_operator_voice_marker_arms_the_gate_on_each_reply_class(
    task_type: str,
) -> None:
    result = _gate(
        task_type, _text("draft_invented_approval_a.txt"), _text("source_no_grant.txt")
    )

    assert _operator_voice_reasons(result), (task_type, result.failure_reasons)


def test_operator_voice_marker_leaves_every_other_class_alone() -> None:
    contract = yaml.safe_load(_CONTRACTS.read_text())
    others = sorted(set(contract["task_classes"]) - set(_REPLY_CLASSES))
    assert others, "positive control: the contract declares classes beyond the four"

    for task_type in others:
        _, heuristic = resolve_task_class_dod_checks(task_type, None)
        assert _CHECK not in heuristic, task_type
        result = _gate(
            task_type,
            _text("draft_invented_approval_a.txt"),
            _text("source_no_grant.txt"),
        )
        assert _operator_voice_reasons(result) == [], task_type
        assert all(e.rule != _CHECK for e in result.rule_evaluations), task_type


def test_operator_voice_marker_handler_hardcodes_no_class_list() -> None:
    handler = (
        Path(__file__).parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_delegation_quality_gate_reducer"
        / "handlers"
        / "operator_voice_check.py"
    ).read_text()

    for task_type in _REPLY_CLASSES:
        assert f'"{task_type}"' not in handler, task_type


# -- the bus path ----------------------------------------------------------------


def _intent(draft: str, source: str) -> ModelQualityGateIntent:
    deterministic, heuristic = resolve_task_class_dod_checks("summarization", source)

    class _Stamped(ModelQualityGateInput):
        grounding_source: str | None = None

    return ModelQualityGateIntent(
        payload=_Stamped(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content=draft,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            grounding_source=source,
        )
    )


def test_operator_voice_bus_intent_handler_refuses_an_invented_approval() -> None:
    result = HandlerQualityGateIntent().handle(
        _intent(_text("draft_invented_approval_a.txt"), _text("source_no_grant.txt"))
    )

    assert result.passed is False
    assert _operator_voice_reasons(result)


def test_operator_voice_bus_intent_handler_accepts_the_plain_summary() -> None:
    result = HandlerQualityGateIntent().handle(
        _intent(_text("draft_plain_summary.txt"), _text("source_no_grant.txt"))
    )

    assert result.passed is True, result.failure_reasons
