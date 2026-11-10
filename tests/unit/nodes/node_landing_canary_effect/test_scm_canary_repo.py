# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``scm`` backend of the landing canary: the commands it runs and how it reads the watcher state (OMN-22920)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_landing_canary_effect.adapters.canary_repo import (
    ScmCanaryRepo,
    parse_state,
)
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelCanaryOpenRequest,
)
from omnimarket.nodes.node_landing_canary_effect.protocols import CanaryRepoError
from tests.unit.nodes.node_landing_canary_effect.canary_support import overlay

pytestmark = pytest.mark.unit

#: The shape of the PR watcher's state for a merged pull request, with the text the reader prints after it.
MERGED_STATE = (
    '{\n "cls": "merged",\n "reason": "merged 2026-11-07T14:42:04Z; last open class pending: 1 pending",\n'
    ' "queued": false\n}\nstate age 120s\n'
)
OPEN_STATE = '{"cls": "pending", "reason": "1 pending: CI Summary"}'
CLOSED_STATE = '{"cls": "closed", "reason": "closed 2026-11-07T14:42:04Z"}'


class Recorder:
    """Records every command and answers from a script by the command's first words."""

    def __init__(self, answers: dict[str, subprocess.CompletedProcess[str]]) -> None:
        self.answers = answers
        self.commands: list[tuple[list[str], Path | None]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path | None, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append((list(argv), cwd))
        for needle, answer in self.answers.items():
            if needle in " ".join(argv):
                return answer
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def done(code: int = 0, out: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, "")


def repo(tmp_path: Path, run: Recorder) -> ScmCanaryRepo:
    scm = overlay().target.scm
    assert scm is not None
    environ = {
        scm.clone_dir_env: str(tmp_path),
        scm.pr_create_command_env: "onex scm pr-create",
        scm.pr_state_command_env: "python3 /opt/pr_state_local.py --json --pr",
    }
    return ScmCanaryRepo(scm, overlay().target.repo, environ, run)


REQUEST = ModelCanaryOpenRequest(
    repo="example-org/example-landing-canary",
    base="main",
    branch="canary/abc123",
    nonce="abc123",
    title="canary: synthetic landing probe abc123",
)


def test_open_pushes_a_one_file_branch_then_opens_the_pull_request(
    tmp_path: Path,
) -> None:
    run = Recorder(
        {
            "rev-parse": done(out="f" * 40 + "\n"),
            "pr-create": done(
                out="published pr-create example-org/example-landing-canary#77\n"
            ),
        }
    )
    opened = repo(tmp_path, run).open(REQUEST)
    assert opened.number == 77
    assert opened.head_sha == "f" * 40
    lines = [" ".join(argv) for argv, _ in run.commands]
    assert lines[0].endswith("fetch origin main")
    assert any(line.endswith("checkout -B canary/abc123 origin/main") for line in lines)
    assert any(line.endswith("push origin canary/abc123") for line in lines)
    create = next(line for line in lines if "pr-create" in line)
    assert "--head canary/abc123 --base main" in create
    assert "example-org/example-landing-canary" in create
    assert (tmp_path / ".canary/abc123.txt").read_text(encoding="utf-8") == "abc123\n"
    pushed = lines.index(next(line for line in lines if " push " in line))
    assert pushed < lines.index(create)


def test_open_that_prints_no_number_is_a_typed_failure(tmp_path: Path) -> None:
    run = Recorder({"rev-parse": done(out="f" * 40), "pr-create": done(out="ok\n")})
    with pytest.raises(CanaryRepoError, match="pr-create-printed-no-number"):
        repo(tmp_path, run).open(REQUEST)


def test_a_refused_push_is_a_typed_failure_and_no_pull_request_is_opened(
    tmp_path: Path,
) -> None:
    run = Recorder({"rev-parse": done(out="f" * 40), " push ": done(1)})
    with pytest.raises(CanaryRepoError, match="git-push-exit-1"):
        repo(tmp_path, run).open(REQUEST)
    assert not any("pr-create" in " ".join(argv) for argv, _ in run.commands)


def test_state_reads_the_watcher_for_the_pull_request(tmp_path: Path) -> None:
    run = Recorder({"pr_state_local": done(out=MERGED_STATE)})
    state = repo(tmp_path, run).state(77)
    assert state.merged is True
    assert state.merged_at == datetime(2026, 11, 7, 14, 42, 4, tzinfo=UTC)
    assert run.commands[0][0][-1] == "example-landing-canary#77"


def test_parse_state_open_and_closed() -> None:
    assert parse_state(OPEN_STATE).merged is False
    assert parse_state(OPEN_STATE).closed is False
    assert parse_state(CLOSED_STATE).closed is True


@pytest.mark.parametrize("text", ["", "no json here", '{"reason": "x"}', "{broken"])
def test_parse_state_refuses_text_that_is_not_a_state(text: str) -> None:
    with pytest.raises(CanaryRepoError):
        parse_state(text)


def test_a_state_command_that_fails_is_a_typed_failure(tmp_path: Path) -> None:
    run = Recorder({"pr_state_local": done(3)})
    with pytest.raises(CanaryRepoError, match="pr-state-exit-3"):
        repo(tmp_path, run).state(77)


def test_a_command_the_environment_does_not_name_refuses_naming_it(
    tmp_path: Path,
) -> None:
    scm = overlay().target.scm
    assert scm is not None
    with pytest.raises(ValueError, match=scm.pr_create_command_env):
        ScmCanaryRepo(
            scm,
            overlay().target.repo,
            {scm.clone_dir_env: str(tmp_path), scm.pr_state_command_env: "x"},
        )
