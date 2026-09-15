#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly job budget must exceed the probe's own derived ceiling (OMN-18349).

The Layer-2 delegation probe paces itself from three declarations it does not
own: how many integration cases the corpus holds, how long the delegate-skill
orchestrator's contract lets one handler run, and how many generations the entry
tier's backends can serve at once. Multiply them and you have the worst-case
wall clock of one corpus run. If the workflow's ``timeout-minutes`` is smaller
than that number, GitHub kills the job mid-corpus -- and it does so BEFORE the
``if: always()`` artifact uploads, so a red night produces no scoreboard, no
JUnit report, and no way to tell a slow lane from a dead one. That is the exact
failure OMN-18349's AC3 exists to prevent, arriving through a different door.

None of the three inputs is a literal here. Each is read from the file that
declares it, so retuning any of them turns this check red instead of silently
making the budget too small:

  * the case count            tests/delegation_golden/corpus.yaml
  * the per-handler bound     node_delegate_skill_orchestrator/contract.yaml
                              (``handler_execution_budget``)
  * the serving concurrency   src/omnimarket/configs/bifrost_delegation.yaml
                              (``saturation_policy.tiers[local]``)

and the arithmetic itself is ``runner.corpus_wall_clock_ceiling_s`` -- the
probe's own function, not a second copy of it. A checker that reimplemented the
formula could agree with a probe that had changed, which is worth nothing.

Runs as the ``nightly-corpus-budget`` pre-commit hook and, through
``tests/test_delegation_nightly_budget.py``, inside the repo's required pytest
job. Same predicate on both sides.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.delegation_golden.corpus_loader import load_corpus  # noqa: E402
from tests.delegation_golden.runner import (  # noqa: E402
    corpus_wall_clock_ceiling_s,
)

NIGHTLY_WORKFLOW = Path(".github/workflows/delegation-regression-nightly.yml")
NIGHTLY_JOB_ID = "golden-tasks"

# Everything the job does that is not the corpus run: checkout, three sibling
# clones, `uv sync`, the preflight, the pytest assertion pass over the written
# scoreboard, the skip guard and both artifact uploads. Measured end to end at
# 62 seconds on run 34810939133 (46s before the corpus, 16s after); five minutes
# is that with room for a cold dependency cache, and it is deliberately a
# generous constant rather than a tight one -- this bound exists to keep the
# artifacts, not to make the job finish quickly.
SETUP_ALLOWANCE_MINUTES = 5


def required_timeout_minutes(case_count: int | None = None) -> int:
    """The smallest ``timeout-minutes`` that lets a full corpus run finish."""
    if case_count is None:
        case_count = len(list(load_corpus().integration_cases()))
    ceiling_s = corpus_wall_clock_ceiling_s(case_count)
    return math.ceil(ceiling_s / 60.0) + SETUP_ALLOWANCE_MINUTES


def declared_timeout_minutes(repo_root: Path | None = None) -> int | None:
    """The ``timeout-minutes`` the nightly job declares, or None if it declares none."""
    root = repo_root or REPO_ROOT
    document = yaml.safe_load((root / NIGHTLY_WORKFLOW).read_text(encoding="utf-8"))
    jobs = document.get("jobs") if isinstance(document, dict) else None
    job = jobs.get(NIGHTLY_JOB_ID) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return None
    declared = job.get("timeout-minutes")
    return declared if isinstance(declared, int) else None


def check_budget(repo_root: Path | None = None) -> list[str]:
    """Return the violations (empty == clean)."""
    required = required_timeout_minutes()
    declared = declared_timeout_minutes(repo_root)
    if declared is None:
        return [
            f"{NIGHTLY_WORKFLOW}: job {NIGHTLY_JOB_ID!r} declares no integer "
            "timeout-minutes, so GitHub applies its 360-minute default and the "
            "job budget is not a reviewed number at all"
        ]
    if declared < required:
        return [
            f"{NIGHTLY_WORKFLOW}: job {NIGHTLY_JOB_ID!r} declares "
            f"timeout-minutes: {declared}, but one full corpus run can take "
            f"{required} minutes (the probe's own derived ceiling plus "
            f"{SETUP_ALLOWANCE_MINUTES} minutes of job setup). A job cancelled "
            "mid-corpus skips its `if: always()` artifact uploads, so the run "
            "leaves no scoreboard and no JUnit report -- the OMN-18349 AC3 "
            "failure, arriving from the job budget instead of from the runner."
        ]
    return []


def main() -> int:
    violations = check_budget()
    if violations:
        print("The delegation nightly's job budget is too small (OMN-18349):")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print(
        "delegation nightly job budget OK "
        f"(declares {declared_timeout_minutes()} minutes, "
        f"needs {required_timeout_minutes()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
