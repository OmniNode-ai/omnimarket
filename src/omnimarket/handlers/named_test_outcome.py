# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Named-test outcome reading: which named tests did not run, and the gate verdict (OMN-25856).

Pytest exit 4 covers both "the named file or test does not exist" (nothing to
run) and "the file exists but cannot be collected" (its import raised, the
delegated code is broken). Only the first is a not-run outcome. Exit 5 (no
tests collected) is always not-run.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.delegation.wire.model_named_test_run import ModelNamedTestRun
from omnimarket.models.delegation.wire.model_quality_gate import (
    INSUFFICIENT_EVIDENCE_VERDICT_PREFIX,
)

_NOT_FOUND_MARKERS = ("file or directory not found", "not found:")
_COLLECTION_BROKEN_MARKERS = ("found no collectors", "error collecting")


def did_not_run(exit_code: int, output: str) -> bool:
    """Whether a nonzero pytest exit means the named test never executed."""
    if exit_code == 5:
        return True
    if exit_code != 4:
        return False
    lowered = output.lower()
    if any(marker in lowered for marker in _COLLECTION_BROKEN_MARKERS):
        return False
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


def run_named_tests(
    test_ids: tuple[str, ...],
    run_one: Callable[[str], tuple[int, str]],
) -> tuple[list[str], int, str]:
    """Run each named test; return ``(failed ids, first exit code, detail)``.

    A test that did not run is not a failure: when none failed and some did
    not run, the detail says so and ``failed`` stays empty.
    """
    failed: list[str] = []
    not_run: list[str] = []
    first_failed: tuple[int, str] | None = None
    first_not_run: tuple[int, str] | None = None
    for test_id in test_ids:
        exit_code, output = run_one(test_id)
        if exit_code == 0:
            continue
        if did_not_run(exit_code, output):
            not_run.append(test_id)
            first_not_run = first_not_run or (exit_code, output[-1500:])
        else:
            failed.append(test_id)
            first_failed = first_failed or (exit_code, output[-1500:])
    first_exit_code, first_output = first_failed or first_not_run or (0, "")
    total = len(test_ids)
    if not failed and not not_run:
        return failed, first_exit_code, f"all {total} named test(s) passed"
    if failed:
        summary = f"{len(failed)} of {total} named test(s) failed: " + ", ".join(failed)
    else:
        summary = (
            f"{len(not_run)} of {total} named test(s) did not run "
            f"(exit {first_exit_code}): " + ", ".join(not_run)
        )
    return failed, first_exit_code, f"{summary[:2499]}\n{first_output}"


def check_passes_existing_tests(run: ModelNamedTestRun) -> str | None:
    """Judge upstream named-test evidence without executing tests (OMN-21076)."""
    if run.passed:
        return None
    if run.not_run:
        # Exit 4 or 5: nothing ran, so the code is neither passed nor failed.
        # INSUFFICIENT, not WEAK_OUTPUT: no costlier rung can supply a test file.
        return (
            f"{INSUFFICIENT_EVIDENCE_VERDICT_PREFIX}: passes_existing_tests: "
            f"{len(run.test_ids)} named test(s) did not run (pytest exit "
            f"{run.exit_code}, usage error or no tests collected; not a code "
            f"failure): {', '.join(run.test_ids)}"
        )
    if run.failed_test_ids:
        return (
            f"WEAK_OUTPUT: passes_existing_tests: {len(run.failed_test_ids)} of "
            f"{len(run.test_ids)} named test(s) failed against the delegated code "
            f"(exit {run.exit_code}): {', '.join(run.failed_test_ids)}"
        )
    return (
        f"WEAK_OUTPUT: passes_existing_tests: the named test run exited "
        f"{run.exit_code} with no passing result for: {', '.join(run.test_ids)}"
    )
