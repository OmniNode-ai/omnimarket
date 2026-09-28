# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every declared task class can finish a delegation (OMN-19406).

A dev-lane sweep on 2026-09-24 ran one real delegation per task class. Two
classes could not complete, whatever the model answered:

* ``validator_generation`` (run 07dc0c4a-f850-4ef9-8de8-13c7b2105382). The
  local answer scored 1.0 and the run was still refused with
  ``required_bar_missing``: the class was the only one in
  ``task_class_contracts.v1.yaml`` with no ``quality_gate`` block, so
  ``resolve_required_bar_authority`` raised on the bus path.
* ``escalation`` (run 0249bf01-b792-4a3e-9fec-d3d84133f5f2). Three local
  answers scored 1.0 and each was refused with the gate's "no deterministic
  acceptance or judge adequacy authority" TASK_MISMATCH, then the ceiling slot
  returned HTTP 429. Its definition of done held only reject-only checks
  (``no_refusal``, ``methodical_analysis``), which can fail an answer but never
  accept one.

A third defect sat in the text the model is given. The Markdown and plain-text
output instructions said "do not include analysis or reasoning before the
deliverable", while ``step_by_step_explanation`` and ``methodical_analysis``
are blocking rules whose directives ask for reasoning in the answer. The
instruction exists to keep a scratchpad out of the answer; it now says that,
and says that any explanation the request asks for belongs inside the
deliverable.

The first two tests are invariants over EVERY declared class, not only the two
that failed, so the next class added without a bar or an acceptance authority
fails here rather than on the dev lane.
"""

from __future__ import annotations

import pytest

from omnimarket.delegation.response_contract_instruction import (
    render_response_contract_instruction,
)
from omnimarket.inference.task_class_authority import resolve_quality_rule
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    RequiredBarAuthorityError,
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _has_adequacy_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_task_class_contract,
    _task_class_entry,
    resolve_task_class_dod_checks,
)


def _declared_task_classes() -> tuple[str, ...]:
    contract = _get_task_class_contract()
    assert contract is not None
    classes = contract.get("task_classes")
    assert isinstance(classes, dict)
    names = tuple(sorted(str(name) for name in classes))
    # Positive control: the contract really is the fifteen-class one, so an
    # empty or truncated read cannot pass the invariants below by vacuity.
    assert "validator_generation" in names
    assert "escalation" in names
    assert len(names) >= 15
    return names


def _routable_task_classes() -> tuple[str, ...]:
    """Classes a delegation can be dispatched for today.

    A class declaring ``routing_availability`` (agent_delegation, OMN-15961)
    resolves no backend on any tier and fails closed before the gate, so the
    acceptance-authority invariant does not apply to it.
    """
    contract = _get_task_class_contract()
    routable: list[str] = []
    for name in _declared_task_classes():
        entry = _task_class_entry(contract, name)
        assert entry is not None
        if "routing_availability" not in entry:
            routable.append(name)
    return tuple(routable)


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _declared_task_classes())
def test_task_class_gaps_every_declared_class_resolves_a_required_bar(
    task_type: str,
) -> None:
    try:
        authority = resolve_required_bar_authority(task_type=task_type)
    except RequiredBarAuthorityError as exc:  # pragma: no cover - the RED case
        pytest.fail(f"{task_type}: {exc}")
    assert 0.0 < authority.required_bar <= 1.0
    assert authority.authority_source == f"task_class:{task_type}"


@pytest.mark.unit
def test_task_class_gaps_validator_generation_bar_matches_its_code_siblings() -> None:
    """Same acceptance authority and DoD family as code_generation, same bar."""
    contract = _get_task_class_contract()
    validator = _task_class_entry(contract, "validator_generation")
    code = _task_class_entry(contract, "code_generation")
    assert validator is not None
    assert code is not None
    assert validator["score_source"] == code["score_source"]
    assert validator["quality_gate"] == code["quality_gate"]
    authority = resolve_required_bar_authority(task_type="validator_generation")
    assert authority.required_bar == pytest.approx(0.85)
    # A caller may still override inside the declared bounds, and not outside.
    assert (
        resolve_required_bar_authority(
            task_type="validator_generation", request_override=0.6
        ).required_bar
        == 0.6
    )
    with pytest.raises(RequiredBarAuthorityError):
        resolve_required_bar_authority(
            task_type="validator_generation", request_override=0.5
        )


@pytest.mark.unit
@pytest.mark.parametrize("task_type", _routable_task_classes())
def test_task_class_gaps_every_routable_class_has_an_acceptance_authority(
    task_type: str,
) -> None:
    deterministic, heuristic = resolve_task_class_dod_checks(task_type)
    assert _has_adequacy_authority(deterministic, heuristic), (
        f"{task_type}: DoD {deterministic + heuristic} holds only reject-only "
        "checks, so no answer can ever pass the quality gate"
    )


# An answer shaped like the one run 0249bf01 refused three times: a methodical,
# complete escalation recommendation with no refusal and no invented identifier.
_ESCALATION_ANSWER = (
    "Escalate the database failover to the on-call platform owner now.\n\n"
    "The replica has been lagging for forty minutes because the primary is "
    "saturated on write I/O, and the automated failover will not trigger while "
    "the replica is behind. Therefore waiting for the automation risks a longer "
    "outage than a supervised manual failover. The evidence is the replication "
    "lag graph and the stalled failover check. The risk of acting is a brief "
    "write interruption; the risk of not acting is losing the primary with a "
    "stale replica."
)


@pytest.mark.unit
def test_task_class_gaps_clean_escalation_answer_passes_the_gate() -> None:
    deterministic, heuristic = resolve_task_class_dod_checks("escalation")
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id="ebe4cdab-c330-4e62-9d5d-d91e30136ba5",
            task_type="escalation",
            llm_response_content=_ESCALATION_ANSWER,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    assert result.passed, result.failure_reasons
    assert (
        result.quality_score
        >= resolve_required_bar_authority(task_type="escalation").required_bar
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "answer",
    [
        "I cannot help with that request.",
        # Complete, but with no reasoning at all: methodical_analysis still vetoes.
        "Escalate the database failover to the on-call platform owner now.",
    ],
)
def test_task_class_gaps_escalation_still_refuses_what_it_refused(answer: str) -> None:
    """Negative control: adding an acceptance authority loosened no floor."""
    deterministic, heuristic = resolve_task_class_dod_checks("escalation")
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id="ebe4cdab-c330-4e62-9d5d-d91e30136ba5",
            task_type="escalation",
            llm_response_content=answer,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )
    assert not result.passed


_TEXT_SHAPES = (("markdown", "### ANSWER"), ("plain_text", "=== ANSWER ==="))


@pytest.mark.unit
@pytest.mark.parametrize(("shape", "marker"), _TEXT_SHAPES)
def test_task_class_gaps_output_instruction_does_not_forbid_requested_reasoning(
    shape: str, marker: str
) -> None:
    rendered = render_response_contract_instruction(
        None, output_shape=shape, render_start_marker=marker
    )
    lowered = rendered.lower()
    # The contradiction: a blanket ban on reasoning, against rules that ask for it.
    assert "do not include analysis or reasoning" not in lowered
    assert "do not include analysis, reasoning" not in lowered
    # What the ban was for is still said: no scratch work ahead of the answer.
    assert "scratch work" in lowered
    assert "before the extraction marker" in lowered
    # And the reasoning a request asks for is placed inside the deliverable.
    assert "belongs inside the deliverable" in lowered
    # The marker sentence is unchanged, since the user turn restates it verbatim.
    assert rendered.endswith(
        "Put this exact extraction start marker on its own line immediately "
        f"before the deliverable: {marker}"
    )


@pytest.mark.unit
def test_task_class_gaps_plain_text_still_forbids_markdown_fencing() -> None:
    rendered = render_response_contract_instruction(
        None, output_shape="plain_text", render_start_marker="=== ANSWER ==="
    )
    assert "markdown fencing" in rendered.lower()


@pytest.mark.unit
def test_task_class_gaps_reasoning_rule_directive_is_not_contradicted() -> None:
    """The directive the gate states for methodical_analysis asks for reasoning.

    Pinned against the rule that already carries a directive on origin/dev, so
    this holds whether or not the step_by_step_explanation directive has landed.
    """
    declared = resolve_quality_rule("methodical_analysis")
    assert declared is not None
    assert declared.model_directive
    assert "reasoning" in declared.model_directive.lower()
    rendered = render_response_contract_instruction(
        None, output_shape="markdown", render_start_marker="### ANSWER"
    ).lower()
    assert "reasoning" in rendered
    assert "belongs inside the deliverable" in rendered
