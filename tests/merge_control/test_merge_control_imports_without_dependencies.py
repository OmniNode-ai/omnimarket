# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``merge_control`` must import under a bare interpreter [OMN-18429].

``scripts/ci/check_merge_reason_codes.py`` says so in its own header — "stdlib
only: it adds the repo ``src`` to ``sys.path`` and imports the classifier" — and
the Merge Reason-Code Gate runs it on a hosted runner with no project
dependencies installed. The outage breaker beside the classifier states the same
invariant for its own reasons: no network, stdlib only, so the whole control
loop is testable with plain lambdas.

Neither statement was enforced. Adding a Pydantic model to this package under
OMN-18429 turned the gate red with a bare ``ModuleNotFoundError: No module named
'pydantic'`` — caught on the pull request rather than in review, which is the
point of writing it down here instead of in a comment. The policy model is a
frozen dataclass for exactly this reason.

The check runs the gate's own import in a subprocess with third-party packages
made unavailable, so it fails the same way the runner does rather than
approximating it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"

# Import exactly what the gate imports, with the site directories that hold the
# project's dependencies removed from the path. `-S` skips site initialisation,
# so only the stdlib and the explicitly added `src` are importable.
_PROGRAM = """
import sys
sys.path.insert(0, {src!r})
from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode
from omnimarket.merge_control.model_outage_breaker_policy import (
    ModelOutageBreakerPolicy,
)
from omnimarket.merge_control.outage_circuit_breaker import OutageCircuitBreaker

policy = ModelOutageBreakerPolicy(
    min_outage_prs=3, min_outage_fraction=0.25, min_window_observations=8
)
assert policy.trips(outage_pr_count=1, observed_pr_count=56) is False
assert policy.trips(outage_pr_count=40, observed_pr_count=56) is True
assert OutageCircuitBreaker(policy=policy).mutations_allowed is True
assert str(EnumMergeCheckReasonCode.GITHUB_API_OUTAGE)
print("ok")
"""


def _run_bare(program: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-S", "-c", program],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(_REPO_ROOT),
    )


def test_the_gates_imports_succeed_without_project_dependencies() -> None:
    result = _run_bare(_PROGRAM.format(src=str(_SRC)))
    assert result.returncode == 0, (
        "The Merge Reason-Code Gate imports this package under a bare "
        "interpreter. A third-party import anywhere in it turns that gate red "
        f"with a ModuleNotFoundError.\nstdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert result.stdout.strip() == "ok"


def test_the_bare_interpreter_really_lacks_the_dependencies() -> None:
    """Positive control. Without it the case above proves only that -S is quiet."""
    result = _run_bare("import pydantic")
    assert result.returncode != 0, (
        "The bare interpreter can still import pydantic, so the case above "
        "would pass even if this package depended on it."
    )
    assert "pydantic" in result.stderr
