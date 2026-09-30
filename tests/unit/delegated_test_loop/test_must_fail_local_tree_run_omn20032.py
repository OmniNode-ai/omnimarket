# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20032: the local runner runs a test against the code before a change.

Real git, real pytest, no fakes below the interpreter provider. The repository
has a bug in one commit and the fix plus its test in the next, which is the shape
of every merged PR the control is asked about.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.delegated_test_loop.must_fail_control import (
    ModelMustFailRunRequest,
    evaluate_must_fail_control,
)
from omnimarket.delegated_test_loop.must_fail_local_tree_run import (
    HandlerMustFailLocalTreeRun,
)
from omnimarket.delegated_test_loop.must_fail_models import (
    EnumMustFailControlOutcome,
    ModelPrChangedFile,
    ModelPrDiffFacts,
)

pytestmark = pytest.mark.unit

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**scrub_git_location_env(os.environ), **_ENV},
    )
    return proc.stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _provider(_repo: Path) -> tuple[Path | None, str | None]:
    return Path(sys.executable), None


def _repo(tmp_path: Path, *, test_passes_before: bool) -> tuple[Path, str, str]:
    repo = tmp_path / "product"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "dev")
    _write(repo, "src/widget/__init__.py", "")
    _write(repo, "src/widget/core.py", "def double(x):\n    return x + x + 1\n")
    _write(repo, "pyproject.toml", "[tool.pytest.ini_options]\ntestpaths = ['tests']\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "buggy")
    pre = _git(repo, "rev-parse", "HEAD")

    _write(repo, "src/widget/core.py", "def double(x):\n    return x + x\n")
    if test_passes_before:
        test = "from widget.core import double\n\n\ndef test_double_zero_is_not_needed():\n    assert double(0) >= 0\n"
    else:
        test = "from widget.core import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
    _write(repo, "tests/test_widget.py", test)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix")
    return repo, pre, _git(repo, "rev-parse", "HEAD")


def _facts(pre: str, change: str) -> ModelPrDiffFacts:
    return ModelPrDiffFacts(
        repo="Org/product",
        pr_number=7,
        merge_commit_sha=change,
        parent_commit_sha=pre,
        changed_files=(
            ModelPrChangedFile(path="src/widget/core.py", status="modified"),
            ModelPrChangedFile(path="tests/test_widget.py", status="added"),
        ),
    )


def _item() -> dict[str, object]:
    return {
        "id": "dod-occ-diff-derived-behavior-proof-pr-7",
        "description": "PR #7 on Org/product — diff-derived behavior proof (OMN-16434).",
        "checks": [],
    }


def _control(repo: Path, pre: str, change: str):  # type: ignore[no-untyped-def]
    return evaluate_must_fail_control(
        item=_item(),
        command="uv run pytest tests/test_widget.py -q",
        facts=_facts(pre, change),
        runner=HandlerMustFailLocalTreeRun(_provider),
        repo_dir=repo,
        timeout_seconds=120,
    )


def test_a_test_that_needs_the_fix_fails_on_the_earlier_code(tmp_path: Path) -> None:
    repo, pre, change = _repo(tmp_path, test_passes_before=False)
    control = _control(repo, pre, change)
    assert control.outcome is EnumMustFailControlOutcome.CONTROLLED, control
    assert control.headline is True
    assert [(r.path, r.outcome) for r in control.runs] == [
        ("tests/test_widget.py", "failed_call")
    ]


def test_a_test_that_passes_before_the_fix_is_vacuous(tmp_path: Path) -> None:
    repo, pre, change = _repo(tmp_path, test_passes_before=True)
    control = _control(repo, pre, change)
    assert control.outcome is EnumMustFailControlOutcome.VACUOUS, control
    assert control.headline is False


def test_the_run_tests_the_earlier_code_not_the_head(tmp_path: Path) -> None:
    """The earlier tree is the import root: the fix at the head is not on the path."""
    repo, pre, change = _repo(tmp_path, test_passes_before=False)
    result = HandlerMustFailLocalTreeRun(_provider).handle(
        ModelMustFailRunRequest(
            repo_dir=repo,
            pre_change_sha=pre,
            change_sha=change,
            test_path="tests/test_widget.py",
            overlay_paths=("tests/test_widget.py",),
            timeout_seconds=120,
        )
    )
    assert result.exit_code == 1
    assert 'failures="1"' in result.junit_xml


def test_a_missing_commit_is_reported_and_runs_nothing(tmp_path: Path) -> None:
    repo, _pre, change = _repo(tmp_path, test_passes_before=False)
    result = HandlerMustFailLocalTreeRun(_provider).handle(
        ModelMustFailRunRequest(
            repo_dir=repo,
            pre_change_sha="c" * 40,
            change_sha=change,
            test_path="tests/test_widget.py",
            timeout_seconds=30,
        )
    )
    assert result.exit_code is None
    assert "not in" in result.detail


def test_no_interpreter_is_reported_and_runs_nothing(tmp_path: Path) -> None:
    repo, pre, change = _repo(tmp_path, test_passes_before=False)
    result = HandlerMustFailLocalTreeRun(lambda _r: (None, "no environment")).handle(
        ModelMustFailRunRequest(
            repo_dir=repo,
            pre_change_sha=pre,
            change_sha=change,
            test_path="tests/test_widget.py",
            timeout_seconds=30,
        )
    )
    assert result.exit_code is None
    assert result.detail == "no environment"


def test_an_interpreter_that_imports_the_head_is_refused(tmp_path: Path) -> None:
    """A run against the head's code would pass for the wrong reason."""
    repo, pre, change = _repo(tmp_path, test_passes_before=False)
    fake = tmp_path / "python-shadow"
    fake.write_text("#!/bin/sh\necho /elsewhere/src/widget/__init__.py\n")
    fake.chmod(0o755)
    result = HandlerMustFailLocalTreeRun(lambda _r: (fake, None)).handle(
        ModelMustFailRunRequest(
            repo_dir=repo,
            pre_change_sha=pre,
            change_sha=change,
            test_path="tests/test_widget.py",
            timeout_seconds=30,
        )
    )
    assert result.exit_code is None
    assert "not from the earlier tree" in result.detail


def test_the_throwaway_tree_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    repo, pre, change = _repo(tmp_path, test_passes_before=False)
    _control(repo, pre, change)
    assert list(scratch.iterdir()) == []
