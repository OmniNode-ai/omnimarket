# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read pytest's closing summary line from a check's output tail (OMN-23637).

The only text read is the summary line the runner already captures (``<counts> in <seconds>s``). pytest's exit codes
carry the rest: 1 is "tests were collected and run, some failed or errored at setup", 2 is "interrupted", which is where
a collection error lands.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_code_contract_score_effect.models.model_pytest_summary import (
    ModelPytestSummary,
)

#: pytest's exit code for a run that completed with a failed test or an error at setup.
PYTEST_EXIT_TESTS_FAILED = 1

_SUMMARY_LINE = re.compile(
    r"^[= ]*(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*) in \d+(?:\.\d+)?s(?: \([^)]*\))?[= ]*$"
)
_COUNT = re.compile(r"(?P<n>\d+) (?P<word>[a-z]+)")


def parse_pytest_summary(output: str) -> ModelPytestSummary | None:
    """The counts on the last summary line of ``output``, or None when it holds none."""
    for line in reversed(output.splitlines()):
        matched = _SUMMARY_LINE.match(line.strip())
        if matched is None:
            continue
        counts = {m["word"]: int(m["n"]) for m in _COUNT.finditer(matched["counts"])}
        return ModelPytestSummary(
            failed=counts.get("failed", 0),
            errors=counts.get("error", 0) + counts.get("errors", 0),
            passed=counts.get("passed", 0),
        )
    return None


def is_setup_error_run(exit_code: int | None, output: str) -> bool:
    """True when the run ended on pytest exit 1 reporting an error and no failed test (a setup fault, not a failure)."""
    if exit_code != PYTEST_EXIT_TESTS_FAILED:
        return False
    summary = parse_pytest_summary(output)
    return summary is not None and summary.ended_in_error_with_no_failed_test


__all__ = ["is_setup_error_run", "parse_pytest_summary"]
