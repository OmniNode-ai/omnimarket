# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFocusedTests: the focused-tests verdict, as a pure function.

Definition-B compute: ``handle(request: ModelFocusedTestsRequest) -> ModelFocusedTestsResult``.
No I/O, no clock. Parses the tests phase text: lines of the form
``tests-not-run <test id> reason=<text to end of line>`` become not-run rows, and
the pytest summary ``N passed`` gives the passed count (0 when absent). A not-run
test is never counted in the passed count and never silently dropped: the
evidence names every not-run id with its reason. The verdict is the existing
judge's conclusion, except that a run in which every selected test is not-run
fails unless the overlay's ``all_not_run_ok`` allows it.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_lab_proof_judge_compute.models.model_focused_tests import (
    ModelFocusedTestsRequest,
    ModelFocusedTestsResult,
    ModelNotRunTest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)

_NOT_RUN_RE = re.compile(r"^tests-not-run\s+(?P<test_id>\S+)\s+reason=(?P<reason>.*)$")
_PASSED_RE = re.compile(r"(?P<count>\d+) passed")


class HandlerFocusedTests:
    """Judge the focused-tests phase from its output text and the overlay."""

    def __init__(self, overlay: ModelLabProofJudgeOverlay) -> None:
        self._overlay = overlay

    def handle(self, request: ModelFocusedTestsRequest) -> ModelFocusedTestsResult:
        """Return the focused-tests verdict for the request's output text."""
        not_run: list[ModelNotRunTest] = []
        for line in request.tests_output.splitlines():
            match = _NOT_RUN_RE.match(line.strip())
            if match is not None:
                not_run.append(
                    ModelNotRunTest(
                        test_id=match.group("test_id"),
                        reason=match.group("reason").strip(),
                    )
                )
        passed_match = _PASSED_RE.search(request.tests_output)
        passed_count = (
            int(passed_match.group("count")) if passed_match is not None else 0
        )

        all_not_run = bool(not_run) and passed_count == 0
        ok = request.base_focused_ok and not (
            all_not_run and not self._overlay.all_not_run_ok
        )

        if not_run:
            evidence = "; ".join(f"{t.test_id} ({t.reason})" for t in not_run)
        else:
            evidence = f"{passed_count} passed"
        return ModelFocusedTestsResult(
            ok=ok,
            passed_count=passed_count,
            not_run=tuple(not_run),
            evidence=evidence,
        )
