#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture-corpus contract gate for the merge-check reason-code classifier.

OMN-14765 (epic OMN-14643). This is the Rule-5 enforcement surface for
``omnimarket.merge_control.reason_code_classifier``: a detection that is not a
pre-merge gate gets ignored, so the classifier ships wired as BOTH a CI job
(``ci.yml`` -> ``merge-reason-code-gate`` -> ``CI Summary`` strict needs) and a
blocking pre-commit hook, invoking THIS same runner.

It replays every captured jobs-API fixture under
``tests/merge_control/fixtures/reason_codes/`` through the classifier and asserts
each classifies to its recorded ``expected_reason_code``. It is fail-closed and
non-vacuous: a missing corpus, a shrunk corpus, or a corpus that no longer covers
all five reason codes FAILS, so a future edit can never silently re-collapse
``cancelled``/``stale``/``infra`` into ``product_failed``.

Stdlib only: it adds the repo ``src`` to ``sys.path`` and imports the classifier,
so it runs under a bare ``setup-python`` step (no ``uv sync``) and under
``uv run`` identically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from omnimarket.merge_control.reason_code_classifier import (  # noqa: E402
    EnumMergeCheckReasonCode,
    classify_dict,
)

_FIXTURE_DIR = _REPO_ROOT / "tests" / "merge_control" / "fixtures" / "reason_codes"
# Non-vacuity floor: the corpus must cover every reason code and carry at least
# this many fixtures, so a collapsed/empty scan cannot pass silently (mirrors the
# OMN-14541 non-vacuity-floor pattern).
_MIN_FIXTURES = 10
_ALL_REASON_CODES = frozenset(str(code) for code in EnumMergeCheckReasonCode)


def _load_fixtures() -> list[tuple[str, dict[str, object]]]:
    if not _FIXTURE_DIR.is_dir():
        raise SystemExit(
            f"FAIL: fixture corpus dir missing: {_FIXTURE_DIR} "
            "(fail-closed — the reason-code gate cannot run without its corpus)"
        )
    fixtures: list[tuple[str, dict[str, object]]] = []
    for path in sorted(_FIXTURE_DIR.glob("*.json")):
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise SystemExit(f"FAIL: fixture {path.name} is not a JSON object")
        fixtures.append((path.name, data))
    return fixtures


def _run_self_check() -> list[str]:
    """Fail-closed default: an unattributed failure must be runner_infra."""
    failures: list[str] = []
    got = classify_dict(
        {
            "job_conclusion": "failure",
            "failed_step_name": "Some unknown gate",
            "run_event": "pull_request",
            "required_context": True,
        }
    )
    if got is not EnumMergeCheckReasonCode.RUNNER_INFRA:
        failures.append(
            "self-check: unattributed failure classified "
            f"{got} (expected runner_infra — fail-closed default broken)"
        )
    # And an unknown conclusion must never be product_failed.
    got2 = classify_dict(
        {"job_conclusion": "mystery_state", "run_event": "pull_request"}
    )
    if got2 is EnumMergeCheckReasonCode.PRODUCT_FAILED:
        failures.append(
            "self-check: unknown conclusion classified product_failed "
            "(fail-closed default broken)"
        )
    return failures


def main(argv: list[str] | None = None) -> int:
    fixtures = _load_fixtures()
    failures: list[str] = []
    seen_codes: set[str] = set()

    for name, data in fixtures:
        expected = str(data.get("expected_reason_code", "")).strip()
        if expected not in _ALL_REASON_CODES:
            failures.append(
                f"{name}: expected_reason_code '{expected}' is not a valid "
                f"EnumMergeCheckReasonCode ({sorted(_ALL_REASON_CODES)})"
            )
            continue
        seen_codes.add(expected)
        got = classify_dict(data)
        if str(got) != expected:
            failures.append(
                f"{name}: classified '{got}' but fixture expects '{expected}' "
                f"— {data.get('description', '')}"
            )

    # Non-vacuity floor.
    if len(fixtures) < _MIN_FIXTURES:
        failures.append(
            f"corpus has {len(fixtures)} fixtures; floor is {_MIN_FIXTURES} "
            "(fail-closed — a shrunk corpus cannot pass)"
        )
    missing = _ALL_REASON_CODES - seen_codes
    if missing:
        failures.append(
            f"corpus does not cover reason code(s) {sorted(missing)} "
            "(fail-closed — every reason code must have a proving fixture)"
        )

    failures.extend(_run_self_check())

    if failures:
        print("merge reason-code gate FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(
        f"merge reason-code gate PASSED: {len(fixtures)} fixtures, "
        f"all {len(_ALL_REASON_CODES)} reason codes covered, fail-closed default OK."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
