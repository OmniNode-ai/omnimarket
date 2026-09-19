# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18820 — a runner crash AFTER a clean session is infra, never product-red.

Measured over 48 h on `product-readiness-shadow`: 7 of 19 failing runs were a
C-extension segfault in garbage collection *after* the slice reported
``15683 passed``. The product was green and the gate elected ``PRODUCT_FAILED``.
A further 4 runs carried genuinely failing tests alongside the same crash.

That asymmetry is the whole design. These tests hold both ends:

* the clean-session crash reaches the infra class (the 7), and
* a crash on top of a failing session does NOT (the 4).

The second half is the one that matters. A rule keyed on the exit code alone
would turn those 4 green, and because `RUNNER_INFRA` deliberately outranks
`PRODUCT_FAILED` in the merge-control classifier, so would a bare
"segmentation fault" log signature. Both shortcuts are refused here by test.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "ci"))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import product_readiness as pr  # noqa: E402
import product_reason_graph as graph  # noqa: E402
import run_shadow_slice as harness  # noqa: E402

from omnimarket.merge_control.reason_code_classifier import (  # noqa: E402
    ALL_LOG_SIGNATURES,
    EnumMergeCheckReasonCode,
    MergeCheckFacts,
    classify,
)

pytestmark = pytest.mark.unit

# The real summary lines, copied verbatim from the field logs this ticket cites.
_CLEAN_SUMMARY = (
    "15683 passed, 176 skipped, 7019 deselected, 1 xfailed, "
    "13 warnings in 775.72s (0:12:55)"
)
_FAILED_SUMMARY = (
    "2 failed, 15718 passed, 176 skipped, 7025 deselected, 1 xfailed, "
    "13 warnings in 755.38s (0:12:35)"
)
_FAULT_TRACE = [
    "Fatal Python error: Segmentation fault",
    "",
    "Current thread 0x00007fd33071d740 (most recent call first):",
    "  Garbage-collecting",
    "  <no Python frame>",
]

_PRODUCT_STEP = "Unit tests (fast slice)"


class TestSessionVerdict:
    """The slice's own summary is the evidence; absence of one proves nothing."""

    def test_clean_summary_reads_clean(self) -> None:
        assert harness.session_verdict([_CLEAN_SUMMARY]) == harness.VERDICT_CLEAN

    def test_failed_summary_reads_failed(self) -> None:
        assert harness.session_verdict([_FAILED_SUMMARY]) == harness.VERDICT_FAILED

    def test_no_summary_reads_unknown_not_clean(self) -> None:
        # Fail closed. A session that never printed a summary has proven nothing,
        # so it must NOT be eligible for the infra class.
        assert harness.session_verdict([]) == harness.VERDICT_UNKNOWN
        assert (
            harness.session_verdict(["collecting ...", "tests/test_x.py .."])
            == harness.VERDICT_UNKNOWN
        )

    def test_progress_chatter_naming_passed_is_not_a_summary(self) -> None:
        # "passed" appears in ordinary output; only the terminal line with its
        # wall-clock counts, or any verbose test name could forge a verdict.
        chatter = ["tests/test_thing.py::test_passed_when_ready PASSED", "1 passed"]
        assert harness.session_verdict(chatter) == harness.VERDICT_UNKNOWN

    def test_the_last_summary_wins(self) -> None:
        assert (
            harness.session_verdict([_FAILED_SUMMARY, _CLEAN_SUMMARY])
            == harness.VERDICT_CLEAN
        )


class TestFatalSignalShape:
    """Both shapes, because a wrapper between us and pytest flattens one."""

    def test_direct_child_reports_negative(self) -> None:
        assert harness.fatal_signal_number(-signal.SIGSEGV) == int(signal.SIGSEGV)

    def test_wrapper_flattened_child_reports_128_plus_n(self) -> None:
        # The live case: `uv run` waits on pytest and translates SIGSEGV to 139,
        # which is why the field logs never show -11.
        assert harness.fatal_signal_number(139) == int(signal.SIGSEGV)

    def test_ordinary_exit_codes_are_not_signals(self) -> None:
        for rc in (0, 1, 2, 5, harness.EXIT_HANG, harness.EXIT_INFRA_CRASH, 127, 128):
            assert harness.fatal_signal_number(rc) is None, rc


class TestExitClassification:
    """AC1 and AC2 — the two halves that must not collapse into one another."""

    def test_clean_session_then_signal_is_the_infra_class(self) -> None:
        rc = harness._classify_exit(139, [_CLEAN_SUMMARY, *_FAULT_TRACE])
        assert rc == harness.EXIT_INFRA_CRASH
        assert rc != 139, "the product-red exit code must not survive"

    def test_failing_session_then_signal_stays_product_red(self) -> None:
        # The negative control: runs 35442252527 / 35441371544 / 35407841155 /
        # 35393490128 all carry real failures AND exit 139. They must stay red.
        rc = harness._classify_exit(139, [_FAILED_SUMMARY, *_FAULT_TRACE])
        assert rc == 139
        assert rc != harness.EXIT_INFRA_CRASH

    def test_signal_with_no_summary_stays_product_red(self) -> None:
        # Fail closed: a crash mid-run, before any summary, is not a clean run.
        rc = harness._classify_exit(139, ["collecting ...", *_FAULT_TRACE])
        assert rc == 139

    def test_a_plain_test_failure_is_untouched(self) -> None:
        assert harness._classify_exit(1, [_FAILED_SUMMARY]) == 1

    def test_success_is_untouched(self) -> None:
        assert harness._classify_exit(0, [_CLEAN_SUMMARY]) == 0

    def test_the_crash_is_reported_loudly_not_swallowed(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # AC4: classifying it non-product is not the same as hiding it.
        harness._classify_exit(139, [_CLEAN_SUMMARY, *_FAULT_TRACE])
        out = capfd.readouterr().out
        assert harness._CRASH_SIGNATURES[0] in out
        assert "SIGSEGV" in out
        assert "OMN-18820" in out


@pytest.mark.skipif(os.name != "posix", reason="signal death is POSIX-only")
class TestEndToEndThroughRunSlice:
    """Drive the real `run_slice`, so the tee and the classifier are both live."""

    def test_real_segfaulting_child_after_clean_summary_is_infra(self) -> None:
        prog = (
            "import ctypes, sys;"
            f"print({_CLEAN_SUMMARY!r}, flush=True);"
            "ctypes.string_at(0)"
        )
        rc = harness.run_slice([sys.executable, "-c", prog], 60.0)
        assert rc == harness.EXIT_INFRA_CRASH

    def test_real_segfaulting_child_after_failed_summary_stays_red(self) -> None:
        prog = (
            "import ctypes, sys;"
            f"print({_FAILED_SUMMARY!r}, flush=True);"
            "ctypes.string_at(0)"
        )
        rc = harness.run_slice([sys.executable, "-c", prog], 60.0)
        assert rc != harness.EXIT_INFRA_CRASH
        assert harness.fatal_signal_number(rc) == int(signal.SIGSEGV)

    def test_child_output_still_reaches_the_job_log(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # The tee must not swallow the log the humans and the classifier read.
        harness.run_slice(
            [sys.executable, "-c", "print('hello from the slice', flush=True)"], 60.0
        )
        assert "hello from the slice" in capfd.readouterr().out


class TestConclusionVocabulary:
    """`runner_crashed` is an INFRA dimension, never a pass and never a fail."""

    def test_runner_crashed_categorizes_infra(self) -> None:
        assert (
            pr.categorize_conclusion("runner_crashed") == pr.EnumSubcheckOutcome.INFRA
        )

    def test_runner_crashed_is_declared_infra_not_merely_fail_closed(self) -> None:
        """The explicit entry is load-bearing; the default is not a substitute.

        Measured while proving these tests bite: deleting ``runner_crashed`` from
        the vocabulary changed NOTHING, because ``categorize_conclusion`` already
        fails closed to INFRA on any word it does not recognise. So the
        behaviour above would pass either way, and on its own it asserts nothing
        about this ticket.

        That default is a safety net for the unknown, not a declaration about a
        conclusion this repo emits on purpose. Two things follow, and this test
        holds both: a reader can tell the vocabulary is intentional rather than
        incidental, and any future tightening of the default -- refusing an
        unrecognised conclusion outright, say -- fails here instead of silently
        re-reddening every crashed run.
        """
        assert "runner_crashed" in pr._INFRA_CONCLUSIONS
        assert "runner_crashed" not in pr._PASS_CONCLUSIONS
        assert "runner_crashed" not in pr._FAIL_CONCLUSIONS
        assert "runner_crashed" not in pr._ABSENT_CONCLUSIONS

    def test_runner_crashed_yields_product_infra_and_blocks_freeze(self) -> None:
        result = pr.classify_dict(
            {
                "change_detection": "success",
                "lint": "success",
                "typecheck": "success",
                "tests": "runner_crashed",
                "coverage": "runner_crashed",
            }
        )
        assert result["outcome"] == pr.PRODUCT_INFRA
        assert result["freeze_eligible"] is False

    def test_a_real_test_failure_still_outranks_infra(self) -> None:
        # AC2 at the classifier layer: an affirmative product failure is not
        # rescued by an infra dimension sitting beside it.
        result = pr.classify_dict(
            {
                "change_detection": "success",
                "lint": "success",
                "typecheck": "success",
                "tests": "failure",
                "coverage": "runner_crashed",
            }
        )
        assert result["outcome"] == pr.TEST_FAILED
        assert result["freeze_eligible"] is False

    def test_product_infra_is_not_an_affirmative_product_failure(self) -> None:
        assert pr.PRODUCT_INFRA not in pr._AFFIRMATIVE_PRODUCT_FAILURES


class TestReasonGraphRoot:
    """AC4 at the graph layer — the crash is classified IN the graph."""

    @staticmethod
    def _graph(tests_conclusion: str) -> dict[str, object]:
        return graph.build_reason_graph(
            {
                "head_sha": "0" * 40,
                "subchecks": {
                    "change_detection": "success",
                    "lint": "success",
                    "typecheck": "success",
                    "tests": tests_conclusion,
                    "coverage": tests_conclusion,
                },
            }
        )

    def test_runner_crashed_elects_runner_infra_not_product_failed(self) -> None:
        root = self._graph("runner_crashed")["root"]
        assert isinstance(root, dict)
        assert root["kind"] == graph.RUNNER_INFRA
        assert root["kind"] != graph.PRODUCT_FAILED

    def test_a_real_failure_still_elects_product_failed(self) -> None:
        root = self._graph("failure")["root"]
        assert isinstance(root, dict)
        assert root["kind"] == graph.PRODUCT_FAILED

    def test_the_crash_is_present_in_the_graph_not_dropped(self) -> None:
        # Not-green is the point: a run that crashed must never read READY.
        built = self._graph("runner_crashed")
        assert built["ready"] is False
        assert built["freeze_eligible"] is False


class TestMergeControllerSeamIsSafe:
    """The signature ranks above PRODUCT_FAILED — prove that cannot be abused."""

    def test_the_crash_signature_is_classifier_recognized(self) -> None:
        assert set(harness._CRASH_SIGNATURES) <= set(ALL_LOG_SIGNATURES)

    def test_a_bare_segfault_string_is_deliberately_not_a_signature(self) -> None:
        # The shortcut this ticket refuses. Adding it would flip the 4 measured
        # runs that segfault AND fail tests from red to green, because infra
        # outranks product failure in this classifier by design.
        lowered = {sig.lower() for sig in ALL_LOG_SIGNATURES}
        assert "segmentation fault" not in lowered
        assert not any("segfault" in sig for sig in lowered)

    def test_the_signature_carries_its_own_precondition_in_its_words(self) -> None:
        # Safety here is structural, not conventional: the phrase names the
        # clean session, and the harness emits it only when the summary was
        # clean, so a failing run cannot produce it.
        assert "clean session" in harness._CRASH_SIGNATURES[0]

    def test_signature_present_classifies_infra(self) -> None:
        facts = MergeCheckFacts(
            job_conclusion="failure",
            failed_step_name=_PRODUCT_STEP,
            log_signatures=tuple(harness._CRASH_SIGNATURES),
        )
        assert classify(facts) == EnumMergeCheckReasonCode.RUNNER_INFRA

    def test_same_step_without_the_signature_is_product_failed(self) -> None:
        facts = MergeCheckFacts(
            job_conclusion="failure",
            failed_step_name=_PRODUCT_STEP,
            log_signatures=(),
        )
        assert classify(facts) == EnumMergeCheckReasonCode.PRODUCT_FAILED
