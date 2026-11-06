# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A new append to an umbrella contract is refused; a pull request's own contract is not."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_ticket_contract_prerequisite import (
    umbrella_appends,
    umbrella_ticket_set,
)

pytestmark = pytest.mark.unit
UMBRELLA = "OMN-19998"


def _environ() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=scrub_git_location_env(_environ()),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _contract(*ids: str) -> str:
    return yaml.safe_dump(
        {"ticket_id": UMBRELLA, "dod_evidence": [{"id": item} for item in ids]}
    )


def _commit(repo: Path, path: str, text: str, message: str) -> None:
    target = repo / path
    target.parent.mkdir(exist_ok=True)
    target.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q", "-b", "dev")
    _commit(tmp_path, f"contracts/{UMBRELLA}.yaml", _contract("dod-old"), "base")
    _git(tmp_path, "checkout", "-q", "-b", "work")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ONEX_UMBRELLA_TICKETS", raising=False)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_error_chain_a_new_append_to_the_umbrella_contract_is_refused(
    repo: Path,
) -> None:
    _commit(repo, f"contracts/{UMBRELLA}.yaml", _contract("dod-old", "dod-new"), "fix")

    failures = umbrella_appends("HEAD", "dev", "Body")

    assert len(failures) == 1
    assert f"contracts/{UMBRELLA}.yaml" in failures[0]
    assert "dod-new" in failures[0]
    assert "child ticket" in failures[0]


def test_golden_chain_a_pull_request_on_its_own_contract_is_admitted(
    repo: Path,
) -> None:
    _commit(repo, "contracts/OMN-1.yaml", _contract("dod-own"), "fix")

    assert umbrella_appends("HEAD", "dev", "Body") == ()


def test_taking_the_base_branch_adds_nothing(repo: Path) -> None:
    _git(repo, "checkout", "-q", "dev")
    _commit(
        repo, f"contracts/{UMBRELLA}.yaml", _contract("dod-old", "dod-landed"), "dev"
    )
    _git(repo, "checkout", "-q", "work")
    _commit(repo, "contracts/OMN-1.yaml", _contract("dod-own"), "fix")
    _git(repo, "merge", "-q", "--no-edit", "dev")

    assert umbrella_appends("HEAD", "dev", "Body") == ()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("Body\n\nUmbrella evidence: this lane holds no ticket access\n", "fix"),
        ("Body", "fix\n\nUmbrella evidence: this lane holds no ticket access"),
    ],
)
def test_an_umbrella_evidence_line_admits_the_append(
    repo: Path, body: str, message: str
) -> None:
    _commit(
        repo, f"contracts/{UMBRELLA}.yaml", _contract("dod-old", "dod-new"), message
    )

    assert umbrella_appends("HEAD", "dev", body) == ()


def test_a_placeholder_evidence_line_does_not_admit_it(repo: Path) -> None:
    _commit(repo, f"contracts/{UMBRELLA}.yaml", _contract("dod-old", "dod-new"), "fix")

    assert umbrella_appends("HEAD", "dev", "Umbrella evidence: <reason>\n")


def test_the_set_names_the_shared_tickets_and_the_variable_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_UMBRELLA_TICKETS", raising=False)
    assert {"OMN-17427", "OMN-19998"} <= set(umbrella_ticket_set())
    monkeypatch.setenv("ONEX_UMBRELLA_TICKETS", "OMN-1, OMN-2")
    assert umbrella_ticket_set() == ("OMN-1", "OMN-2")
