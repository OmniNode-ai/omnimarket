# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour tests for the ladder's scorers (OMN-18300).

Each scorer is tested with a positive control and at least one negative: a
scorer that can only ever return PASS would make every model look competent,
which is the exact failure the mutation and pre-fix controls exist to prevent,
so the scorers themselves get the same treatment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.models import EnumOutcome, ModelTaskBundle
from benchmarks.delegation_ladder.scorers import (
    extract_code,
    extract_identifiers,
    grounding_report,
    score_diagnosis_location,
    score_exact_match,
    score_grounding_coverage,
    score_grounding_rubric,
    score_patch_apply_and_test,
    score_unit_test_execution,
    split_answer,
)

HERE = Path(__file__).resolve().parent.parent
FIXTURES = HERE / "fixtures"
BUNDLES = HERE / "bundles"

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Response anatomy
# --------------------------------------------------------------------------


def test_a_clean_response_has_no_leak() -> None:
    answer, leak = split_answer("The dev issuer host is the dev-prefixed one.")
    assert answer == "The dev issuer host is the dev-prefixed one."
    assert leak == 0.0


def test_a_closed_reasoning_trace_is_stripped_and_measured() -> None:
    answer, leak = split_answer("<think>" + "x" * 90 + "</think>\nthe answer")
    assert answer == "the answer"
    assert leak > 0.8


def test_an_unterminated_trace_leaves_no_answer() -> None:
    answer, leak = split_answer("Here's a thinking process:\n1. consider the input")
    assert answer == ""
    assert leak == 1.0


def test_an_answer_heading_ends_the_trace_when_no_tag_closes_it() -> None:
    answer, leak = split_answer(
        "Here's a thinking process:\nstep one\nstep two\n\nFinal answer:\nport 8085"
    )
    assert answer == "port 8085"
    assert 0.0 < leak < 1.0


def test_prose_mentioning_the_word_answer_is_not_treated_as_a_heading() -> None:
    text = "The answer depends on the lane the probe named."
    answer, leak = split_answer(text)
    assert answer == text
    assert leak == 0.0


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------


def test_identifiers_of_every_guarded_shape_are_extracted() -> None:
    found = extract_identifiers(
        "OMN-18297 and omnibase_core#1678 and dev.auth.omninode.ai"
    )
    assert "OMN-18297" in found
    assert "omnibase_core#1678" in found
    assert "dev.auth.omninode.ai" in found


def test_an_identifier_absent_from_the_input_is_reported_as_invented() -> None:
    fraction, ungrounded = grounding_report("OMN-1 and OMN-2", "only OMN-1 was fed")
    assert ungrounded == ["OMN-2"]
    assert fraction == 0.5


def test_an_answer_with_no_identifiers_invents_nothing() -> None:
    fraction, ungrounded = grounding_report("a plain sentence", "anything")
    assert (fraction, ungrounded) == (1.0, [])


# --------------------------------------------------------------------------
# R1 and R2
# --------------------------------------------------------------------------

_R1_CONFIG: dict[str, object] = {
    "source_text": "mint against dev.auth.omninode.ai with client onex-verify",
    "must_contain": ["dev.auth.omninode.ai"],
    "must_not_contain": ["against auth.omninode.ai"],
    "max_words": 30,
}


def test_r1_accepts_a_correct_short_rewrite() -> None:
    verdict = score_grounding_rubric(
        "Mint a token against dev.auth.omninode.ai using client onex-verify.",
        _R1_CONFIG,
    )
    assert verdict.outcome is EnumOutcome.PASS
    assert verdict.mechanical


def test_r1_rejects_a_rewrite_that_kept_the_wrong_host() -> None:
    verdict = score_grounding_rubric(
        "Mint a token against auth.omninode.ai using client onex-verify.", _R1_CONFIG
    )
    assert verdict.outcome is EnumOutcome.FAIL


def test_r1_rejects_an_over_long_rewrite() -> None:
    verdict = score_grounding_rubric(
        "Mint a token against dev.auth.omninode.ai. " + "padding " * 40, _R1_CONFIG
    )
    assert verdict.outcome is EnumOutcome.FAIL
    assert verdict.checklist["at_most_30_words"] is False


def test_r2_refuses_a_summary_that_invented_one_identifier() -> None:
    verdict = score_grounding_coverage(
        "landed OMN-1 and OMN-9999",
        {
            "source_text": "OMN-1 OMN-2",
            "required_ids": ["OMN-1"],
            "coverage_threshold": 0.5,
        },
    )
    assert verdict.outcome is EnumOutcome.FAIL
    assert verdict.checklist["no_invented_identifiers"] is False


def test_r2_accepts_a_grounded_summary_that_covers_enough() -> None:
    verdict = score_grounding_coverage(
        "landed OMN-1 and OMN-2",
        {
            "source_text": "OMN-1 OMN-2 OMN-3",
            "required_ids": ["OMN-1", "OMN-2", "OMN-3"],
            "coverage_threshold": 0.6,
        },
    )
    assert verdict.outcome is EnumOutcome.PASS


# --------------------------------------------------------------------------
# R3 and R5
# --------------------------------------------------------------------------


def test_r3_matches_across_whitespace_and_smart_quotes() -> None:
    verdict = score_exact_match(
        "It returns   [‘foo’].", {"accepted": ["['foo']"], "forbidden": []}
    )
    assert verdict.outcome is EnumOutcome.PASS


def test_r3_rejects_a_forbidden_alternative() -> None:
    verdict = score_exact_match(
        "It returns [].", {"accepted": ["['foo']"], "forbidden": ["[]"]}
    )
    assert verdict.outcome is EnumOutcome.FAIL


def test_r5_requires_the_defective_site_not_merely_the_failing_test() -> None:
    config: dict[str, object] = {
        "must_name": ["extract_imports"],
        "any_of": ["split"],
        "must_not_name": [],
    }
    assert (
        score_diagnosis_location(
            "test_full_dotted_paths_are_returned fails.", config
        ).outcome
        is EnumOutcome.FAIL
    )
    assert (
        score_diagnosis_location(
            "extract_imports truncates each name with split on the dot.", config
        ).outcome
        is EnumOutcome.PASS
    )


def test_r5_rejects_an_answer_that_blames_the_decoy() -> None:
    verdict = score_diagnosis_location(
        "extract_imports splits wrongly, and is_valid_onex_name is also broken.",
        {
            "must_name": ["extract_imports"],
            "any_of": ["split"],
            "must_not_name": ["is_valid_onex_name"],
        },
    )
    assert verdict.outcome is EnumOutcome.FAIL


# --------------------------------------------------------------------------
# Code extraction
# --------------------------------------------------------------------------


def test_the_largest_fenced_block_wins_over_an_illustrative_fragment() -> None:
    answer = "first:\n```python\nx = 1\n```\nreal one:\n```python\ndef f():\n    return 2\n```"
    assert extract_code(answer).strip() == "def f():\n    return 2"


def test_an_unfenced_answer_is_taken_whole() -> None:
    assert extract_code("def f():\n    return 2").strip() == "def f():\n    return 2"


# --------------------------------------------------------------------------
# R4 -- the mutation control
# --------------------------------------------------------------------------

_R4_CONFIG: dict[str, object] = {
    "subject_file": "r4_02_subject.py",
    "mutant_file": "r4_02_mutant.py",
    "timeout_s": 90,
}


def test_r4_credits_a_test_that_passes_the_real_function_and_fails_the_mutant() -> None:
    good = (
        "```python\n"
        "from subject import detect_add_remove_conflicts\n\n\n"
        "def test_case_insensitive_by_default():\n"
        '    assert detect_add_remove_conflicts(["Foo"], ["foo"], "h") == ["foo"]\n'
        "```"
    )
    verdict = score_unit_test_execution(good, _R4_CONFIG, FIXTURES)
    assert verdict.outcome is EnumOutcome.PASS
    assert verdict.checklist["fails_the_mutant"] is True


def test_r4_refuses_a_test_that_cannot_tell_the_mutant_apart() -> None:
    vacuous = (
        "```python\n"
        "from subject import detect_add_remove_conflicts\n\n\n"
        "def test_disjoint_lists():\n"
        '    assert detect_add_remove_conflicts(["a"], ["b"], "h") == []\n'
        "```"
    )
    verdict = score_unit_test_execution(vacuous, _R4_CONFIG, FIXTURES)
    assert verdict.outcome is EnumOutcome.FAIL
    assert verdict.checklist["passes_the_real_function"] is True
    assert verdict.checklist["fails_the_mutant"] is False
    assert "does not discriminate" in verdict.detail


def test_r4_refuses_a_response_carrying_no_code() -> None:
    verdict = score_unit_test_execution("I would write a test.", _R4_CONFIG, FIXTURES)
    assert verdict.outcome is EnumOutcome.FAIL
    assert verdict.checklist["produced_code"] is False


# --------------------------------------------------------------------------
# R6 -- the pre-fix control
# --------------------------------------------------------------------------

_R6_CONFIG: dict[str, object] = {
    "prelude_file": "r6_01_prelude.py",
    "prefix_body_file": "r6_01_prefix.py",
    "focused_test_file": "r6_01_test.py",
    "timeout_s": 120,
}


def test_r6_credits_the_real_fix() -> None:
    fixed = (
        "```python\n"
        '_OCC_REPO_NAME = "onex_change_control"\n'
        '_OCC_REPO_ORG = "OmniNode-ai"\n'
        '_OCC_REPO_QUALIFIED = f"{_OCC_REPO_ORG}/{_OCC_REPO_NAME}"\n\n\n'
        "def _is_occ_repo(repo: str) -> bool:\n"
        '    normalized = repo.strip().rstrip("/")\n'
        "    return normalized in (_OCC_REPO_NAME, _OCC_REPO_QUALIFIED)\n"
        "```"
    )
    verdict = score_patch_apply_and_test(fixed, _R6_CONFIG, FIXTURES)
    assert verdict.outcome is EnumOutcome.PASS


def test_r6_refuses_a_patch_that_leaves_the_defect_in_place() -> None:
    unchanged = (
        "```python\n"
        + (FIXTURES / "r6_01_prefix.py").read_text(encoding="utf-8")
        + "```"
    )
    verdict = score_patch_apply_and_test(unchanged, _R6_CONFIG, FIXTURES)
    assert verdict.outcome is EnumOutcome.FAIL


def test_r6_reports_a_harness_error_when_the_focused_test_cannot_discriminate() -> None:
    """A focused test that passes the pre-fix source voids its own task."""
    verdict = score_patch_apply_and_test(
        "```python\nx = 1\n```",
        {**_R6_CONFIG, "focused_test_file": "r6_01_prelude.py"},
        FIXTURES,
    )
    assert verdict.outcome is EnumOutcome.HARNESS_ERROR


# --------------------------------------------------------------------------
# The committed bundles
# --------------------------------------------------------------------------


def test_every_committed_bundle_parses_and_names_its_fixtures() -> None:
    paths = sorted(BUNDLES.glob("*.json"))
    assert len(paths) == 28, "seven rungs of four tasks"
    for path in paths:
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        assert bundle.prompt.strip(), f"{bundle.task_id} has an empty prompt"
        assert bundle.provenance.strip(), f"{bundle.task_id} has no provenance"
        for name in bundle.fixture_files:
            assert (FIXTURES / name).is_file(), (
                f"{bundle.task_id} names a missing {name}"
            )


def test_every_rung_carries_four_tasks() -> None:
    counts: dict[str, int] = {}
    for path in sorted(BUNDLES.glob("*.json")):
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        counts[bundle.rung.value] = counts.get(bundle.rung.value, 0) + 1
    assert counts == {
        "R1": 4,
        "R2": 4,
        "R3": 4,
        "R3b": 4,
        "R4": 4,
        "R5": 4,
        "R6": 4,
    }


def test_every_exact_match_task_holds_at_least_one_accepted_form() -> None:
    """A held answer with no accepted form would pass nothing, silently."""
    for path in sorted(BUNDLES.glob("*.json")):
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if bundle.scorer.value != "exact_match":
            continue
        accepted = bundle.scorer_config.get("accepted")
        assert isinstance(accepted, list), f"{bundle.task_id} accepted is not a list"
        assert accepted, f"{bundle.task_id} accepts nothing"


def test_no_diagnosis_task_can_be_passed_by_echoing_its_own_trace() -> None:
    """R5 feeds the failing trace. If every token the scorer accepts already
    appears in that trace, a model that copies the trace back scores a pass
    without having diagnosed anything."""
    for path in sorted(BUNDLES.glob("*.json")):
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if bundle.scorer.value != "diagnosis_location":
            continue
        trace_start = bundle.prompt.index("PYTEST OUTPUT:")
        trace = bundle.prompt[trace_start:].lower()
        raw = bundle.scorer_config.get("any_of")
        any_of = [str(x).lower() for x in raw] if isinstance(raw, list) else []
        assert any(token not in trace for token in any_of), (
            f"{bundle.task_id} accepts only tokens already present in its own trace"
        )


def test_every_r2_coverage_target_actually_occurs_in_its_fed_rows() -> None:
    """A coverage target absent from the input would be unreachable by any model."""
    for path in sorted(BUNDLES.glob("*.json")):
        bundle = ModelTaskBundle.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if bundle.scorer.value != "grounding_coverage":
            continue
        source = str(bundle.scorer_config["source_text"])
        raw_required = bundle.scorer_config.get("required_ids")
        required = (
            [str(x) for x in raw_required] if isinstance(raw_required, list) else []
        )
        assert required, f"{bundle.task_id} requires nothing"
        for token in required:
            assert token in source, f"{bundle.task_id} requires unreachable {token}"


# --------------------------------------------------------------------------
# The committed evidence
# --------------------------------------------------------------------------

RESULTS = HERE / "results"


def test_every_committed_result_has_an_intact_transcript() -> None:
    """A score must be re-derivable from the exact bytes that produced it.

    This is not decoration. The transcripts were first written as plain text and
    the repository's whitespace-fixing hooks rewrote fifty of fifty-six of them
    on the first commit attempt, which would have left every score pointing at a
    hash of text that no longer existed. They are JSON now so the bytes survive,
    and this test is what would catch it happening again.
    """
    import hashlib

    merged = sorted(RESULTS.glob("merged-*.json"))
    if not merged:
        pytest.skip("no merged result table committed yet")
    checked = 0
    for table in merged:
        path_name = table.stem.removeprefix("merged-")
        for record in json.loads(table.read_text(encoding="utf-8"))["records"]:
            transcript = RESULTS / "responses" / path_name / f"{record['task_id']}.json"
            assert transcript.is_file(), f"no transcript for {record['task_id']}"
            blob = json.loads(transcript.read_text(encoding="utf-8"))
            digest = hashlib.sha256(blob["response"].encode("utf-8")).hexdigest()
            assert digest == record["response_sha256"], (
                f"{path_name}/{record['task_id']}: the committed transcript is not "
                "the text the score was computed from"
            )
            assert blob["sha256"] == digest
            checked += 1
    assert checked > 0


def test_no_result_row_claims_a_non_mechanical_verdict() -> None:
    """Every scorer in this harness is mechanical; a row saying otherwise is a bug."""
    for table in sorted(RESULTS.glob("merged-*.json")):
        for record in json.loads(table.read_text(encoding="utf-8"))["records"]:
            assert record["score"]["mechanical"] is True, (
                f"{record['task_id']} claims a non-mechanical verdict"
            )
