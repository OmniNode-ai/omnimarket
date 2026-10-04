# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The v1 acceptance rubric: its classes, its explicit rules and its calibration provenance."""

import re

import pytest

from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.parse_rubric import (
    parse_rubric,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)
from tests.nodes.node_delegation_acceptance_judge_compute.builders import (
    EDIT_LOOP_TASK,
    RUBRIC,
    RUBRIC_YAML,
    item,
    render,
)

pytestmark = pytest.mark.unit

CLASSES = {
    "summarization",
    "review",
    "document",
    "code_generation",
    "code_review",
    "test",
    "reasoning",
    "edit_loop_turn",
}


def test_rubric_is_the_versioned_v1_with_every_task_class() -> None:
    assert RUBRIC.rubric_version == "delegation-acceptance-judge.v1"
    assert set(RUBRIC.classes) == CLASSES
    for key, class_rubric in RUBRIC.classes.items():
        assert class_rubric.accept, key
        assert class_rubric.reject_notes, key


def test_rubric_failure_classes_in_the_prompt_match_the_enum() -> None:
    from_rubric = {c.failure_class for c in RUBRIC.judge_prompt.reject_conditions}
    assert from_rubric <= set(EnumAcceptanceFailureClass)
    named = RUBRIC.judge_prompt.failure_class_rule
    for member in EnumAcceptanceFailureClass:
        assert member.value in named


def test_rubric_states_the_permitted_reply_rule() -> None:
    rule = RUBRIC.judge_prompt.permitted_reply_rule
    assert "cannot execute" in rule
    assert "ACCEPT" in rule


def test_rubric_makes_an_invented_fact_an_automatic_reject() -> None:
    fabrication = next(
        c.text
        for c in RUBRIC.judge_prompt.reject_conditions
        if c.failure_class is EnumAcceptanceFailureClass.FABRICATION
    )
    assert "automatic reject" in fabrication


def test_rubric_excludes_liveness_probes() -> None:
    probe_tasks = [
        "Reply with the single word: alive. Nothing else.",
        "reply with the single word: ok",
        "Reply with exactly the word: alive",
        "say ok",
    ]
    prose = "Summarise the three facts below in two sentences."
    items = [item(f"p{i}", task=t, task_type="test") for i, t in enumerate(probe_tasks)]
    result = render([*items, item("real", task=prose)])
    assert set(result.excluded_probe_ids) == {f"p{i}" for i in range(len(probe_tasks))}
    judged = {i for batch in result.batches for i in batch.item_ids}
    assert judged == {"real"}


def test_rubric_probe_rule_does_not_swallow_a_long_task_that_mentions_one_word() -> (
    None
):
    long_task = "Reply with the single word that names the class. " + "context " * 60
    result = render([item("long", task=long_task), item("real")])
    assert result.excluded_probe_ids == ()


def test_rubric_provenance_records_the_calibration_numbers() -> None:
    provenance = RUBRIC.provenance
    assert provenance.calibration_set.n == 76
    by_key = {(j.judge, j.item_set): j for j in provenance.judges}
    assert by_key[("opus", "all")].kappa == pytest.approx(0.68)
    assert by_key[("opus", "all")].agreement == pytest.approx(0.842)
    assert (
        by_key[("opus", "all")].false_accepts,
        by_key[("opus", "all")].false_rejects,
    ) == (5, 7)
    assert by_key[("codex", "all")].kappa == pytest.approx(0.61)
    assert (
        by_key[("codex", "all")].false_accepts,
        by_key[("codex", "all")].false_rejects,
    ) == (4, 11)
    assert by_key[("opus_vs_codex", "all_calibration")].kappa == pytest.approx(0.82)
    assert by_key[
        ("delegate_quality_gate", "matrix_trials")
    ].agreement == pytest.approx(0.545)
    assert provenance.corpus.events == 7901
    assert provenance.corpus.volume_weighted_accept_rate == pytest.approx(0.57)
    assert provenance.corpus.volume_terminal_ok_rate == pytest.approx(0.96)


def test_rubric_provenance_records_the_label_noise_cases() -> None:
    cases = {c.item: c for c in RUBRIC.provenance.label_noise.cases}
    assert len(cases) == 10
    assert {"t12", "t13", "R4b", "t7", "t1"} <= set(cases)
    assert "cannot-execute" in cases["t12"].finding


def test_rubric_thresholds_match_the_calibration() -> None:
    thresholds = RUBRIC.thresholds
    assert thresholds.kappa_min == pytest.approx(0.6)
    assert thresholds.double_judge_fraction == pytest.approx(0.10)
    assert thresholds.min_judged_per_cell == 15
    assert thresholds.accept_min_quality == 2


def test_rubric_prompt_carries_only_the_classes_in_the_batch() -> None:
    result = render([item("a", task_type="summarization"), item("b", task_type="test")])
    prompt = result.batches[0].prompt
    assert "Class summarization:" in prompt
    assert "Class test:" in prompt
    assert "Class code_review:" not in prompt
    edit = render([item("e", task_type="code_generation", task=EDIT_LOOP_TASK)])
    assert "Class edit_loop_turn:" in edit.batches[0].prompt
    assert "kind: edit_loop_turn" in edit.batches[0].prompt


def test_rubric_parse_refuses_a_non_mapping_and_an_unknown_field() -> None:
    with pytest.raises(ValueError, match="mapping"):
        parse_rubric("- not a mapping\n")
    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        parse_rubric(RUBRIC_YAML + "surprise: true\n")


def test_rubric_file_has_no_unrendered_placeholder() -> None:
    result = render([item("a")])
    assert not re.search(r"\{reason_max_chars\}", result.batches[0].prompt)
    assert "200 characters" in result.batches[0].prompt
