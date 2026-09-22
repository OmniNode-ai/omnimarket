# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19194 — a shadow-slice timeout is infra, never product-red.

Sibling of ``test_omn18820_shadow_runner_crash_class.py``: that ticket wired
exit 125 (``EXIT_INFRA_CRASH``) into the infra vocabulary; this one wires exit
124 (``EXIT_HANG``, the harness's own hang/isolation code — see
``run_shadow_slice.py``'s docstring, unchanged by this ticket) the same way.

Measured over the last 30 completed runs of `product-readiness-shadow.yml`'s
`tests+coverage (shadow)` job on 2026-09-22 (via the Actions API, job logs
read with ``--allow-escape-sequences``, never with stderr suppressed):

* 10 of 30 runs failed. Every one of the 10 carried the harness's own
  ``[run_shadow_slice] HARD TIMEOUT after 900s`` line and ``exit code 124`` in
  its job log, and none carried a failing-test summary line — a runner-speed
  fact, not a product defect (run ids 35784675025, 35776225571, 35773417101,
  35759176830, 35755344489, 35754877537, 35740262658, 35728382944,
  35715886226, 35699793251).
* Successful runs ran up to 931s of total job wall-time, and one success
  run's "Unit tests (fast slice)" step alone ran for exactly 900s (run
  35719304102) before finishing inside the old budget — evidence the 900s
  budget was already running against its own wall on a normal day, not only
  on a slow one.

Before this fix, the workflow step's exit-124 branch fell through to the
generic ``failure`` conclusion (see ``product-readiness-shadow.yml``), so
``reason-graph`` and ``product-readiness / evaluate`` reported a false
``PRODUCT_FAILED`` root for a hang/timeout that the harness's own docstring
already called "not a product failure". These tests hold the vocabulary and
graph-root side of that fix; the harness's own exit-124 classification
(``EXIT_HANG``) is unchanged and already covered by ``test_run_shadow_slice.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts" / "ci"))

import product_readiness as pr  # noqa: E402
import product_reason_graph as graph  # noqa: E402
import run_shadow_slice as harness  # noqa: E402

pytestmark = pytest.mark.unit


class TestTimeoutConclusionVocabulary:
    """`timeout` is an INFRA dimension, never a pass and never a fail."""

    def test_timeout_categorizes_infra(self) -> None:
        assert pr.categorize_conclusion("timeout") == pr.EnumSubcheckOutcome.INFRA

    def test_timeout_is_declared_infra_not_merely_fail_closed(self) -> None:
        """The explicit entry is load-bearing; the default is not a substitute.

        Mirrors ``test_runner_crashed_is_declared_infra_not_merely_fail_closed``
        (OMN-18820). ``categorize_conclusion`` already fails closed to INFRA on
        any word it does not recognise, so deleting this entry would change
        nothing about the assertions above on their own — they would pass
        either way and would assert nothing about this ticket. This test reads
        the vocabulary set directly, so it fails if the entry is removed even
        though the fail-closed default would silently cover for it.
        """
        assert "timeout" in pr._INFRA_CONCLUSIONS
        assert "timeout" not in pr._PASS_CONCLUSIONS
        assert "timeout" not in pr._FAIL_CONCLUSIONS
        assert "timeout" not in pr._ABSENT_CONCLUSIONS

    def test_timeout_yields_product_infra_and_blocks_freeze(self) -> None:
        result = pr.classify_dict(
            {
                "change_detection": "success",
                "lint": "success",
                "typecheck": "success",
                "tests": "timeout",
                "coverage": "timeout",
            }
        )
        assert result["outcome"] == pr.PRODUCT_INFRA
        assert result["freeze_eligible"] is False

    def test_a_real_test_failure_still_outranks_timeout(self) -> None:
        # A genuine product failure is not rescued by an infra dimension
        # sitting beside it, same as the runner_crashed negative control.
        result = pr.classify_dict(
            {
                "change_detection": "success",
                "lint": "success",
                "typecheck": "success",
                "tests": "failure",
                "coverage": "timeout",
            }
        )
        assert result["outcome"] == pr.TEST_FAILED
        assert result["freeze_eligible"] is False

    def test_product_infra_is_not_an_affirmative_product_failure(self) -> None:
        assert pr.PRODUCT_INFRA not in pr._AFFIRMATIVE_PRODUCT_FAILURES


class TestTimeoutReasonGraphRoot:
    """The timeout is classified IN the graph, not dropped."""

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

    def test_timeout_elects_runner_infra_not_product_failed(self) -> None:
        root = self._graph("timeout")["root"]
        assert isinstance(root, dict)
        assert root["kind"] == graph.RUNNER_INFRA
        assert root["kind"] != graph.PRODUCT_FAILED

    def test_a_real_failure_still_elects_product_failed(self) -> None:
        root = self._graph("failure")["root"]
        assert isinstance(root, dict)
        assert root["kind"] == graph.PRODUCT_FAILED

    def test_the_timeout_is_present_in_the_graph_not_dropped(self) -> None:
        # Not-green is the point: a timed-out run must never read READY.
        built = self._graph("timeout")
        assert built["ready"] is False
        assert built["freeze_eligible"] is False


class TestHarnessDocstringStillNamesExitHangNonProduct:
    """Sanity: this fix wires the vocabulary to a code path the harness itself
    already declared non-product. If a future edit ever changed EXIT_HANG's
    meaning, this ticket's premise (124 == a hang, not a defect) would need
    re-checking, so pin the constant this test suite's own header cites.
    """

    def test_exit_hang_is_124(self) -> None:
        assert harness.EXIT_HANG == 124
