# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m`` entry (``runtime_gate_accept``) of the gate-accept node: the form the workflow step runs from the base commit.

The step runs this module with ``PYTHONPATH`` on a worktree of the pull request's base
commit, so the code that decides is never the pull request's own. The entry reads its
input from flags, builds the handler with the production ports (or the ones a test gives
it) and writes the decision to the step's output and summary files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect.runtime_gate_accept import (
    main,
)

from .support import (
    CREDIT_ERROR,
    HEAD,
    PR,
    PUBLISHER,
    REPO,
    FakeReviews,
    FakeVerifier,
    accepted,
    degraded_with,
    receipt_review,
)


def flags(tmp_path: Path, *, head: str = HEAD) -> list[str]:
    return [
        "--repository",
        REPO,
        "--pull-request",
        str(PR),
        "--head-sha",
        head,
        "--voter-result-path",
        str(tmp_path / "result.json"),
        "--step-output-path",
        str(tmp_path / "output"),
        "--step-summary-path",
        str(tmp_path / "summary"),
    ]


def test_the_entry_decides_and_writes_the_step_files(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    code = main(
        flags(tmp_path),
        reviews=FakeReviews([receipt_review("r-1", author=PUBLISHER)]),
        verifier=FakeVerifier({"r-1": accepted("r-1")}),
    )
    assert code == 0
    output = (tmp_path / "output").read_text().splitlines()
    assert "decision=pass_agent" in output
    assert "receipt_id=r-1" in output
    assert "decision=pass_agent" in (tmp_path / "summary").read_text()


def test_the_entry_refuses_without_an_overlay_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    (tmp_path / "result.json").write_text(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    assert main(flags(tmp_path)) == 0
    assert "decision=refuse" in (tmp_path / "output").read_text()


def test_an_input_the_node_cannot_accept_exits_two_and_writes_nothing(
    tmp_path: Path,
) -> None:
    assert main(flags(tmp_path, head="not-a-sha")) == 2
    assert not (tmp_path / "output").exists()


def test_the_module_runs_as_python_dash_m() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_agent_review_gate_accept_effect.runtime_gate_accept",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--voter-result-path" in completed.stdout
