# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stale origin/dev must not hand other PRs' paths and tickets to the pushed change (OMN-21887).

A landing worker's clone can hold an ``origin/dev`` many hours behind the
branch it pushes. The merge base then sits far behind the pushed change, so
every runtime path and every ticket of the pull requests that landed in
between used to be charged to this push: 320 runtime paths and 19 foreign
tickets for a change of seven paths and one ticket. The push owns the commits
that GitHub has not squash-merged; the rest are dev's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers import (
    handler_ticket_contract_prerequisite as boundary,
)

pytestmark = pytest.mark.unit

OWN = "OMN-21887"
FOREIGN = "OMN-10001"
GITHUB = "noreply@github.com"
LANE = "lane@example.com"

UNIT_ITEM = """
  - id: dod-unit
    description: unit tests
    source: manual
    binds_ac: [AC1]
    checks:
      - check_type: test_passes
        check_value: uv run pytest tests/test_x.py -q
"""

PROOF_ITEM = """
  - id: dod-integration-proof
    description: the change ran on a lab host with real dependencies
    source: manual
    execution_scope: local_done_gate
    binds_ac: [AC1]
    checks:
      - check_type: command
        check_value: "uv run onex lab-work --lane l --repo omnimarket --ref b -- uv run pytest tests/test_x.py"
"""


def _contract(ticket: str, *items: str) -> str:
    return (
        f"ticket_id: {ticket}\n"
        "requirements:\n"
        "  - id: req\n"
        "    statement: s\n"
        "    acceptance:\n"
        "      - id: AC1\n"
        "        statement: 'AC1: x -- falsifier: uv run pytest tests/test_x.py -q'\n"
        "dod_evidence:" + "".join(items)
    )


class Repo:
    """A scratch repository whose commits carry a chosen committer, like GitHub's squash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "dev")

    def git(self, *args: str, committer: str = LANE) -> str:
        identity = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": committer,
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": committer,
        }
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
            env=scrub_git_location_env({**os.environ, **identity}),
        ).stdout.strip()

    def commit(self, subject: str, files: dict[str, str], committer: str = LANE) -> str:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.git("add", ".")
        self.git("commit", "-q", "-m", subject, committer=committer)
        return self.git("rev-parse", "HEAD")

    def squash_to_dev(self, number: int, name: str) -> str:
        """What GitHub leaves on dev: a runtime change committed by GitHub, subject ending (#N)."""
        return self.commit(
            f"feat({FOREIGN}): {name} (#{number})",
            {f"src/omnimarket/{name}.py": "x = 1\n"},
            committer=GITHUB,
        )


@pytest.fixture(autouse=True)
def _no_inherited_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    """dev moves on by two squash merges after the clone's origin/dev was last read."""
    repo = Repo(tmp_path)
    repo.commit("base", {"README.md": "x\n"})
    repo.git("tag", "stale-origin-dev")
    repo.squash_to_dev(1, "dev_one")
    repo.squash_to_dev(2, "dev_two")
    monkeypatch.chdir(tmp_path)
    return repo


def _push_commit(repo: Repo, files: dict[str, str]) -> str:
    repo.git("checkout", "-q", "-b", "OMN-21887-work")
    return repo.commit(f"feat({OWN}): change", files)


def test_a_stale_base_does_not_charge_dev_paths_or_tickets_to_a_docs_push(
    stale: Repo,
) -> None:
    head = _push_commit(
        stale,
        {
            f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM),
            "docs/plan.md": "plan\n",
        },
    )
    assert boundary.check(head, "stale-origin-dev") == ()


def test_a_stale_base_still_refuses_the_pushed_runtime_change_alone(
    stale: Repo,
) -> None:
    head = _push_commit(
        stale,
        {
            f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM),
            "src/omnimarket/own.py": "x = 1\n",
        },
    )
    failures = boundary.check(head, "stale-origin-dev")
    assert len(failures) == 1
    assert OWN in failures[0]
    assert "needs an integration proof" in failures[0]
    assert "1 runtime path(s): src/omnimarket/own.py" in failures[0]
    assert "dev_one" not in failures[0]
    assert "dev_two" not in failures[0]


def test_a_stale_base_accepts_the_pushed_runtime_change_with_its_proof(
    stale: Repo,
) -> None:
    head = _push_commit(
        stale,
        {
            f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM, PROOF_ITEM),
            "src/omnimarket/own.py": "x = 1\n",
        },
    )
    assert boundary.check(head, "stale-origin-dev") == ()


def test_a_branch_that_merged_dev_in_is_charged_only_its_own_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub's update-branch merge brings dev in as the second parent."""
    repo = Repo(tmp_path)
    repo.commit("base", {"README.md": "x\n"})
    repo.git("tag", "stale-origin-dev")
    repo.git("checkout", "-q", "-b", "OMN-21887-work")
    repo.commit(
        f"feat({OWN}): change",
        {
            f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM),
            "docs/plan.md": "plan\n",
        },
    )
    repo.git("checkout", "-q", "dev")
    repo.squash_to_dev(1, "dev_one")
    repo.git("checkout", "-q", "OMN-21887-work")
    repo.git(
        "merge", "-q", "--no-ff", "-m", "Merge branch 'dev'", "dev", committer=GITHUB
    )
    monkeypatch.chdir(tmp_path)
    assert boundary.check("HEAD", "stale-origin-dev") == ()


def test_a_merge_that_resolves_a_conflict_into_runtime_code_is_still_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repo(tmp_path)
    repo.commit("base", {"README.md": "x\n", "shared.txt": "a\n"})
    repo.git("tag", "stale-origin-dev")
    repo.git("checkout", "-q", "-b", "OMN-21887-work")
    repo.commit(
        f"feat({OWN}): change",
        {f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM), "shared.txt": "own\n"},
    )
    repo.git("checkout", "-q", "dev")
    repo.commit(
        f"feat({FOREIGN}): shared (#1)", {"shared.txt": "dev\n"}, committer=GITHUB
    )
    repo.git("checkout", "-q", "OMN-21887-work")
    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "dev"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
        check=False,
    )
    (tmp_path / "shared.txt").write_text("resolved\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "evil.py").write_text("x = 1\n")
    repo.git("add", ".")
    repo.git("commit", "-q", "-m", "Merge branch 'dev'", committer=GITHUB)
    monkeypatch.chdir(tmp_path)
    failures = boundary.check("HEAD", "stale-origin-dev")
    assert len(failures) == 1
    assert "src/evil.py" in failures[0]


def test_a_commit_the_lane_wrote_with_a_squash_looking_subject_is_still_its_own(
    stale: Repo,
) -> None:
    """GitHub alone commits a squash merge; a lane's subject ending (#N) proves nothing."""
    stale.git("checkout", "-q", "-b", "OMN-21887-work")
    stale.commit(
        f"feat({OWN}): change (#7)",
        {
            f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM),
            "src/omnimarket/own.py": "x = 1\n",
        },
    )
    failures = boundary.check("HEAD", "stale-origin-dev")
    assert len(failures) == 1
    assert "own.py" in failures[0]


def test_a_current_base_gives_the_same_verdict_as_a_stale_one(stale: Repo) -> None:
    head = _push_commit(
        stale,
        {
            f"contracts/{OWN}.yaml": _contract(OWN, UNIT_ITEM),
            "src/omnimarket/own.py": "x = 1\n",
        },
    )
    assert boundary.check(head, "stale-origin-dev") == boundary.check(head, "dev")
