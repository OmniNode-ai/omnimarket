# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21469: the customer-never-lab routing tests leave no routing global behind.

``test_omn17082_customer_never_lab_backend`` routes through ``delta``, which binds
the handler's process-global ``_config`` (the tier ladder) from the contract the
test wrote, and caches the task-class contract. Its fixture cleared only the
bifrost endpoint cache, so ``test_delegation_tier_escalation`` run after it in
the same pytest process read a ``cheap_cloud`` ladder where it expected
``cheap_frontier`` (3 failed, 63 passed). The reverse order passed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
POLLUTER = "tests/test_omn17082_customer_never_lab_backend.py"
VICTIM = "tests/unit/delegation/test_delegation_tier_escalation.py"


def _run(*files: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *files,
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:randomly",
            "--no-cov",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=480,
        check=False,
    )


def test_the_customer_never_lab_file_does_not_change_the_tier_escalation_tests_after_it() -> (
    None
):
    """Run the two files in the order that failed; the second must still pass."""
    result = _run(POLLUTER, VICTIM)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]


def test_the_tier_escalation_tests_pass_before_the_customer_never_lab_file() -> None:
    """The reverse order passed on dev and must keep passing."""
    result = _run(VICTIM, POLLUTER)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
