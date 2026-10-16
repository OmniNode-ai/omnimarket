# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main-target guard admits no PR onto ``main`` on a change-control receipt (OMN-20074).

``main`` is release-synced: release.yml's "Sync main to release tag" step
advances it, and no pull request is opened against it. The guard used to admit
a ``dev`` or ``promotion/*`` head carrying ``promotion-receipt: OCC-<n>`` and a
``hotfix/*`` head carrying ``hotfix-evidence: OCC-<n>`` plus a backmerge link.
Those receipts were records in the change-control repository, which is being
retired, and the operator ruling of 2026-10-10T16:52:56Z holds that anything
targeting ``main`` violates the rule. The guard therefore refuses every pull
request onto ``main``, receipt or not, and still admits any pull request onto
``dev`` (the negative control).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "main-target-guard.yml"


def _guard_program() -> str:
    workflow = yaml.safe_load(GUARD_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["main-target-guard"]["steps"]
    named = [step for step in steps if step.get("name") == "Check PR target branch"]
    assert len(named) == 1
    return str(named[0]["run"])


def _run_guard(base: str, head: str, body: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "BASE_REF": base,
        "HEAD_REF": head,
        "PR_BODY": body,
    }
    return subprocess.run(
        ["bash", "-e", "-c", _guard_program()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


RECEIPT_SHAPES = [
    ("dev", "promotion-receipt: OCC-123"),
    ("promotion/2026-10-15", "promotion-receipt: OCC-123"),
    ("hotfix/urgent", "hotfix-evidence: OCC-123\nbackmerge: #42"),
    ("feature/x", "promotion-receipt: OCC-123"),
]


@pytest.mark.parametrize(("head", "body"), RECEIPT_SHAPES)
def test_receipt_does_not_admit_a_pr_onto_main(head: str, body: str) -> None:
    result = _run_guard("main", head, body)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Retarget this PR to dev" in result.stdout


@pytest.mark.parametrize(("head", "body"), RECEIPT_SHAPES)
def test_any_pr_onto_dev_is_admitted(head: str, body: str) -> None:
    result = _run_guard("dev", head, body)
    assert result.returncode == 0, result.stdout + result.stderr


def test_guard_reads_no_change_control_receipt() -> None:
    program = _guard_program()
    assert "OCC-" not in program
    assert "PR_BODY" not in program
