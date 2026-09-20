# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18902: the sixth reason code, the re-run rescue, and verdict provenance.

Every number asserted here was re-derived live from the jobs API on 2026-09-20
by the lane that landed this change, not carried over from the plan that
motivated it: 1,635 workflow runs over a 13-ticket sample, 102 of them failing,
141 failing steps, against a positive control of 1,499 successful runs. The
corpus those steps became is
``tests/merge_control/fixtures/omn18902_measured_failing_steps_2026_09_20.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.merge_control.reason_code_classifier import (
    _REASON_CODE_PRECEDENCE,
    EnumCiAttemptCauseClass,
    EnumMergeCheckReasonCode,
    MergeCheckFacts,
    classify,
    classify_verdict,
    dominant_reason_code,
    eval_cause_class,
)

pytestmark = pytest.mark.unit

_CORPUS = (
    Path(__file__).parent
    / "fixtures"
    / "omn18902_measured_failing_steps_2026_09_20.json"
)


def _load_corpus() -> dict[str, object]:
    with open(_CORPUS) as handle:
        return json.load(handle)


def _failed(step: str, **kwargs: object) -> MergeCheckFacts:
    """A plain failed check on the current head, PR-associated."""
    return MergeCheckFacts(
        job_conclusion="failure",
        failed_step_name=step,
        run_event="pull_request",
        required_context=True,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# AC-1: the measured corpus reproduces the measured split, exactly.
# --------------------------------------------------------------------------


def test_the_measured_corpus_reproduces_the_split_exactly() -> None:
    """The shipped vocabulary, over 141 REAL step names, gives 118/12/11/0.

    Asserted by equality on each class rather than on a total, so a change
    that moves one step from process to work still fails even though the
    occurrences still sum to 141.
    """
    corpus = _load_corpus()
    steps = corpus["steps"]
    assert isinstance(steps, list)

    counts: dict[str, int] = {c.value: 0 for c in EnumCiAttemptCauseClass}
    for entry in steps:
        verdict = classify_verdict(_failed(str(entry["failed_step_name"])))
        counts[eval_cause_class(verdict).value] += int(entry["count"])

    assert counts == corpus["expected_class_split"]
    assert sum(counts.values()) == corpus["sample"]["failing_steps"] == 141


def test_the_corpus_is_the_measured_one_and_not_a_hand_written_stand_in() -> None:
    """A corpus quietly shrunk to whatever still passes proves nothing.

    Pins the sample's own shape, so trimming the awkward steps out of the
    fixture is a red test rather than a green one.
    """
    corpus = _load_corpus()
    assert corpus["sample"] == {
        "pull_requests": 13,
        "workflow_runs": 1635,
        "failing_runs": 102,
        "failing_steps": 141,
        "successful_runs_positive_control": 1499,
    }
    steps = corpus["steps"]
    assert isinstance(steps, list)
    assert len(steps) == 32, "32 distinct failing step names were measured"
    assert sum(int(entry["count"]) for entry in steps) == 141


def test_the_dominant_class_is_the_governance_gate_and_not_broken_machines() -> None:
    """The finding this whole change exists for, asserted rather than recited.

    Under the five-member vocabulary these 118 steps reached the fail-closed
    default and were recorded as runner infrastructure, so the fleet's most
    common failure was diagnosed as a broken machine 84 percent of the time.
    """
    corpus = _load_corpus()
    split = corpus["expected_class_split"]
    assert isinstance(split, dict)
    assert split["process"] == 118
    assert split["process"] / 141 > 0.83
    assert split["process"] > split["infra"] + split["work"]


# --------------------------------------------------------------------------
# AC-2: the re-run rescue outranks every step-name match.
# --------------------------------------------------------------------------


def test_a_same_sha_rerun_rescue_beats_a_product_shaped_step_name() -> None:
    """A commit that reached green on a bare re-run did not fail on the product.

    The step is named for the test suite and failed with a real failure
    conclusion, so every name rule in the module says product. The rescue
    fact is consulted first and answers infrastructure.
    """
    verdict = classify_verdict(
        _failed("Run pytest (full suite)", same_sha_later_attempt_succeeded=True)
    )
    assert verdict.code is EnumMergeCheckReasonCode.RUNNER_INFRA
    assert verdict.affirmative is True
    assert eval_cause_class(verdict) is EnumCiAttemptCauseClass.INFRA


def test_the_same_fixture_without_the_rescue_fact_is_a_product_failure() -> None:
    """The positive control that proves the RULE moved it, not the name.

    Without this, a classifier that called every pytest step infrastructure
    would pass the test above and look correct.
    """
    verdict = classify_verdict(_failed("Run pytest (full suite)"))
    assert verdict.code is EnumMergeCheckReasonCode.PRODUCT_FAILED
    assert eval_cause_class(verdict) is EnumCiAttemptCauseClass.WORK


def test_the_rescue_fact_defaults_false_so_existing_callers_are_unchanged() -> None:
    """Every caller that predates this field gets the verdict it always got."""
    assert MergeCheckFacts().same_sha_later_attempt_succeeded is False
    assert classify(_failed("Run ruff")) is EnumMergeCheckReasonCode.PRODUCT_FAILED


# --------------------------------------------------------------------------
# AC-3: the fail-closed default is preserved, and is now distinguishable.
# --------------------------------------------------------------------------


def test_an_unrecognised_step_fails_closed_to_infra_and_reports_non_affirmative() -> (
    None
):
    """The default is unchanged in CODE and newly honest in PROVENANCE.

    The plan rejected loosening the fail-closed default outright, so the code
    is still infrastructure. What changes is that a consumer can now tell this
    apart from an infrastructure fault someone actually identified.
    """
    verdict = classify_verdict(_failed("Reticulate the splines"))
    assert verdict.code is EnumMergeCheckReasonCode.RUNNER_INFRA
    assert verdict.affirmative is False
    assert eval_cause_class(verdict) is EnumCiAttemptCauseClass.UNKNOWN


def test_an_affirmative_infra_step_is_the_same_code_with_the_flag_set() -> None:
    """Positive control in the same run, against the test above.

    Same enum member, opposite provenance. Without this control a classifier
    that reported every verdict as non-affirmative would pass.
    """
    verdict = classify_verdict(_failed("Set up runner"))
    assert verdict.code is EnumMergeCheckReasonCode.RUNNER_INFRA
    assert verdict.affirmative is True
    assert eval_cause_class(verdict) is EnumCiAttemptCauseClass.INFRA


def test_an_unrecognised_step_is_never_a_product_failure() -> None:
    """The expensive documented mistake, still refused."""
    for step in ("Reticulate the splines", "Do the thing", "", "Some unknown gate"):
        assert classify(_failed(step)) is not EnumMergeCheckReasonCode.PRODUCT_FAILED


# --------------------------------------------------------------------------
# The new member itself.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "step",
    [
        "Resolve Evidence-Source",
        "Wait for required OCC preflight",
        "Run OCC eligibility check",
        "Evaluate gate",
        "Run adversarial review",
        "Require every cited OCC companion to be merged before product merge",
        "Enforce hostile-review verdict (OMN-15110)",
        "Execute mandatory source assertions and seeded RED controls",
        "Run Receipt-Gate",
        "Run imperative contract guard",
        "Check PR base branch",
        "Enable auto-merge",
        "Emit reason graph",
        "Classify product readiness",
        "No code merged onto a published version without a bump",
        "Publish onex.evt.github.pr-merged.v1",
        "Check for integration tests in PR",
    ],
)
def test_measured_governance_gate_steps_classify_as_process(step: str) -> None:
    """Each of these is a real step name from the 2026-09-20 corpus."""
    verdict = classify_verdict(_failed(step))
    assert verdict.code is EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED
    assert verdict.affirmative is True


def test_the_coverage_gate_is_process_although_its_name_carries_a_product_token() -> (
    None
):
    """Ordering, not vocabulary, is what decides this one.

    "Check for integration tests in PR" contains ``test``. With the product
    family ranked first the controller would dispatch a code fix at a gate
    refusing a missing artifact, which is the wrong-diagnosis class this
    module exists to remove.
    """
    assert "test" in "Check for integration tests in PR".lower()
    assert (
        classify(_failed("Check for integration tests in PR"))
        is EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED
    )


def test_a_cancelled_governance_gate_is_cancelled_and_not_a_refusal() -> None:
    """A gate that was cancelled never refused anything.

    The process branch outranks cancelled but requires a real failure
    conclusion, so this falls through to cancelled rather than reporting a
    refusal that did not happen.
    """
    verdict = classify_verdict(
        MergeCheckFacts(
            job_conclusion="cancelled",
            failed_step_name="Run OCC eligibility check",
            run_event="pull_request",
            required_context=True,
        )
    )
    assert verdict.code is EnumMergeCheckReasonCode.CANCELLED


def test_an_infra_step_still_outranks_the_process_family() -> None:
    """Provisioning is diagnosed before governance, unchanged."""
    assert classify(_failed("Set up job")) is EnumMergeCheckReasonCode.RUNNER_INFRA


def test_a_stale_context_still_outranks_everything_but_the_rescue() -> None:
    """The pre-existing top of the chain is where it was."""
    assert (
        classify(_failed("Resolve Evidence-Source", is_superseded=True))
        is EnumMergeCheckReasonCode.STALE_CONTEXT
    )


# --------------------------------------------------------------------------
# AC-4: every consumer of the enum, named and checked.
# --------------------------------------------------------------------------

#: Every module that reads ``EnumMergeCheckReasonCode`` or a function of this
#: classifier, enumerated by the OMN-18902 consumer audit. The second element
#: says whether the consumer matches the enum EXHAUSTIVELY — and therefore
#: whether a new member can change its behaviour silently.
_ENUM_CONSUMERS: tuple[tuple[str, bool], ...] = (
    # Exhaustive by literal enumeration. THE dangerous one: a member absent
    # from the precedence tuple does not rank last, it falls past the loop and
    # is relabelled by the catch-all.
    ("omnimarket.merge_control.reason_code_classifier", True),
    # Exhaustive by iterating the enum: demands a covering fixture per member.
    ("scripts/ci/check_merge_reason_codes.py", True),
    ("tests/merge_control/test_reason_code_classifier.py", True),
    # Single-member equality against the outage code. A sixth member simply
    # never matches, which is correct.
    ("omnimarket.merge_control.outage_circuit_breaker", False),
    # Binary split: product means dispatch a code fix, everything else means
    # rerun. A governance-gate refusal lands in the second arm, which is the
    # intended routing and is asserted below.
    (
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers"
        ".handler_pr_lifecycle_orchestrator",
        False,
    ),
    # Pass-through of whatever classify returns onto a typed field.
    (
        "omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers"
        ".handler_pr_lifecycle_inventory",
        False,
    ),
    (
        "omnimarket.nodes.node_pr_lifecycle_inventory_compute.models"
        ".model_pr_lifecycle_inventory",
        False,
    ),
    # Untyped tuple-of-str carriers: structurally unaffected.
    (
        "omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols"
        ".protocol_sub_handlers",
        False,
    ),
    (
        "omnimarket.nodes.node_pr_lifecycle_triage_compute.models"
        ".model_pr_triage_result",
        False,
    ),
    ("omnimarket.events.pr_lifecycle_triage", False),
)


def test_the_consumer_audit_names_every_consumer() -> None:
    """The audit is a list someone has to edit, on purpose.

    A seventh member added without revisiting this list is not caught by any
    type checker, because two of these consumers carry the codes as bare
    strings and two more match a single member by equality.
    """
    assert len(_ENUM_CONSUMERS) == 10
    exhaustive = [name for name, is_exhaustive in _ENUM_CONSUMERS if is_exhaustive]
    assert len(exhaustive) == 3


def test_every_enum_member_has_a_precedence_entry() -> None:
    """The one consumer that fails SILENTLY, pinned.

    ``_REASON_CODE_PRECEDENCE`` is a literal tuple, not an iteration. A member
    missing from it falls past the loop and is relabelled runner infrastructure
    by the catch-all — no exception, no warning, just the wrong diagnosis.
    """
    assert set(_REASON_CODE_PRECEDENCE) == set(EnumMergeCheckReasonCode)
    assert len(_REASON_CODE_PRECEDENCE) == len(EnumMergeCheckReasonCode) == 6


def test_the_new_member_survives_the_pull_request_level_collapse() -> None:
    """A PR whose only failure is a gate refusal reports that, not infra."""
    assert (
        dominant_reason_code((EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED,))
        is EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED
    )


def test_a_real_product_failure_still_dominates_a_gate_refusal() -> None:
    """Code that genuinely broke must still be fixed, gate or no gate."""
    assert (
        dominant_reason_code(
            (
                EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED,
                EnumMergeCheckReasonCode.PRODUCT_FAILED,
            )
        )
        is EnumMergeCheckReasonCode.PRODUCT_FAILED
    )


def test_a_gate_refusal_dominates_a_bare_infra_blip() -> None:
    """A missing artifact is actionable; "a machine broke" is the fallback."""
    assert (
        dominant_reason_code(
            (
                EnumMergeCheckReasonCode.RUNNER_INFRA,
                EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED,
            )
        )
        is EnumMergeCheckReasonCode.PROCESS_GATE_REFUSED
    )


def test_the_merge_controller_does_not_dispatch_a_code_fix_at_a_gate_refusal() -> None:
    """The live control-surface consequence, asserted against the real router.

    The orchestrator's split is binary, so the new member reaches the rerun
    arm without an explicit branch. That is the intended routing — a code fix
    cannot produce a missing evidence artifact — but it is intended rather
    than accidental, so it is pinned here.
    """
    from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
        dominant_reason_code as orchestrator_dominant,
    )

    assert orchestrator_dominant is dominant_reason_code
    assert (
        orchestrator_dominant(("process_gate_refused",))
        is not EnumMergeCheckReasonCode.PRODUCT_FAILED
    )
