# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the pr_polish pre-commit failure signal (OMN-13587, RH-5).

These cover:
- a simulated pre-commit failure producing a structured signal with the
  failing hook id(s) and reported file path(s),
- a passing pre-commit leaving the signal absent,
- the completed-event carrying the structured field on the failure path,
- the auto-merge arming block source being byte-identical (unchanged).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_polish import workflow_runner
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_completed_event import (
    ModelPrPolishCompletedEvent,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_precommit_failure import (
    ModelPrPolishPrecommitFailure,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)

_FAILED_OUTPUT = """\
ruff.....................................................................Failed
- hook id: ruff
- exit code: 1

src/omnimarket/foo.py:3:1: F401 'os' imported but unused
src/omnimarket/foo.py:4:1: F401 'sys' imported but unused

trim trailing whitespace.................................................Failed
- hook id: trailing-whitespace
- files were modified by this hook

Fixing src/omnimarket/bar.py
"""


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.mark.unit
def test_run_precommit_failure_captures_structured_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(returncode=1, stdout=_FAILED_OUTPUT)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    with pytest.raises(workflow_runner.PrecommitFailureError) as exc_info:
        workflow_runner._run_precommit_all_files(Path("/nonexistent/worktree"))

    failure = exc_info.value.failure
    assert isinstance(failure, ModelPrPolishPrecommitFailure)
    assert failure.exit_code == 1
    assert failure.command == "uv run pre-commit run --all-files"
    # Failing hook ids parsed from the "- hook id: <id>" lines.
    assert "ruff" in failure.hook_ids
    assert "trailing-whitespace" in failure.hook_ids
    # File paths the failing hooks reported are extracted with diagnostic
    # line:col noise stripped (so a ruff "<path>:3:1:" line yields the path).
    assert "src/omnimarket/foo.py" in failure.paths
    assert "src/omnimarket/bar.py" in failure.paths
    # Bounded raw tail preserved as a fallback signal.
    assert "Failed" in failure.output_tail
    assert len(failure.output_tail) <= workflow_runner._PRECOMMIT_OUTPUT_TAIL_CHARS


@pytest.mark.unit
def test_run_precommit_pass_raises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_run(*_args: object, **_kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(returncode=0, stdout="all hooks passed\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Returns normally (None); no PrecommitFailureError raised on a clean run.
    assert (
        workflow_runner._run_precommit_all_files(Path("/nonexistent/worktree")) is None
    )


@pytest.mark.unit
def test_parse_precommit_failure_dedupes_and_extracts() -> None:
    hook_ids, paths = workflow_runner._parse_precommit_failure(_FAILED_OUTPUT)
    assert hook_ids == ("ruff", "trailing-whitespace")
    assert "src/omnimarket/bar.py" in paths


@pytest.mark.unit
def test_completed_event_carries_precommit_failure_on_failure_path() -> None:
    failure = ModelPrPolishPrecommitFailure(
        command="uv run pre-commit run --all-files",
        exit_code=1,
        hook_ids=("ruff",),
        paths=("src/omnimarket/foo.py",),
        output_tail="ruff....Failed",
    )
    from datetime import UTC, datetime

    completed = ModelPrPolishCompletedEvent(
        correlation_id=uuid4(),
        final_phase=EnumPrPolishPhase.FAILED,
        started_at=datetime.now(tz=UTC),
        completed_at=datetime.now(tz=UTC),
        pr_number=42,
        error_message="boom",
        precommit_failure=failure,
    )
    dumped = completed.model_dump(mode="json")
    assert dumped["precommit_failure"]["hook_ids"] == ["ruff"]
    assert dumped["precommit_failure"]["paths"] == ["src/omnimarket/foo.py"]


@pytest.mark.unit
def test_completed_event_precommit_failure_absent_by_default() -> None:
    from datetime import UTC, datetime

    completed = ModelPrPolishCompletedEvent(
        correlation_id=uuid4(),
        final_phase=EnumPrPolishPhase.DONE,
        started_at=datetime.now(tz=UTC),
        completed_at=datetime.now(tz=UTC),
        pr_number=42,
    )
    assert completed.precommit_failure is None
    assert completed.model_dump(mode="json")["precommit_failure"] is None


@pytest.mark.unit
def test_auto_merge_arming_block_unchanged() -> None:
    """The arming logic must stay byte-identical (no behavior change, OMN-13587)."""
    source = Path(workflow_runner.__file__).read_text()
    arming_block = (
        "            if coderabbit_result.has_blockers:\n"
        '                payload["auto_merge_status"] = "blocked_by_coderabbit"\n'
        "            elif command.no_automerge:\n"
        '                payload["auto_merge_status"] = "skipped"\n'
        "            else:\n"
        "                _enable_auto_merge(command.repo, command.pr_number)\n"
        '                payload["auto_merge_status"] = "armed"\n'
    )
    assert arming_block in source
