# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A merge of dev is judged on what the pull request itself adds (OMN-23283).

The lab hook pre-check (and any caller that runs the push-stage hook before the
commit exists) hands the hook a commit of the staged tree with the branch head as
its ONLY parent, so a clean merge of dev reads as one commit that carries every
dev change since the fork. The prerequisite judged dev's umbrella entries and
dev's runtime paths as the pull request's own and refused a clean merge.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_ticket_contract_prerequisite import (
    check,
)

pytestmark = pytest.mark.unit
OWN = "OMN-23283"
UMBRELLA = "OMN-19998"
TITLE = f"fix({OWN}): change"


def _git(repo: Path, *args: str, check_rc: bool = True) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=scrub_git_location_env(env),
        check=check_rc,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _umbrella(*ids: str) -> str:
    return yaml.safe_dump(
        {"ticket_id": UMBRELLA, "dod_evidence": [{"id": item} for item in ids]}
    )


def _own_contract() -> str:
    checks = [
        {"check_type": "test_passes", "check_value": "uv run pytest tests/test_x.py -q"}
    ]
    return yaml.safe_dump(
        {
            "ticket_id": OWN,
            "requirements": [{"acceptance": [{"id": "AC1", "statement": "check"}]}],
            "dod_evidence": [{"id": "unit", "binds_ac": ["AC1"], "checks": checks}],
        }
    )


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)


def _staged_commit(repo: Path) -> str:
    """The commit the lab hook pre-check builds: the staged tree on top of HEAD alone."""
    tree = _git(repo, "write-tree")
    return _git(
        repo,
        "-c",
        "user.name=hook-preflight",
        "-c",
        "user.email=hook-preflight@invalid",
        "commit-tree",
        tree,
        "-p",
        "HEAD",
        "-m",
        "staged",
    )


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own: dict[str, str]) -> Path:
    """dev holds the umbrella contract; the PR forks it and adds ``own``; dev then moves on."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "dev")
    _commit(
        tmp_path,
        {f"contracts/{UMBRELLA}.yaml": _umbrella("dod-old"), "README.md": "x\n"},
        "base",
    )
    _git(tmp_path, "checkout", "-q", "-b", "OMN-23283-work")
    _commit(tmp_path, own, f"fix({OWN}): change")
    _git(tmp_path, "checkout", "-q", "dev")
    _commit(
        tmp_path,
        {
            f"contracts/{UMBRELLA}.yaml": _umbrella("dod-old", "dod-landed"),
            "src/dev_runtime.py": "x = 1\n",
        },
        "feat(OMN-1): landed on dev",
    )
    _git(tmp_path, "checkout", "-q", "OMN-23283-work")
    _git(tmp_path, "merge", "-q", "--no-commit", "--no-ff", "dev", check_rc=False)
    return tmp_path


def _docs_only() -> dict[str, str]:
    return {f"contracts/{OWN}.yaml": _own_contract(), "docs/change.md": "change\n"}


def test_golden_chain_a_staged_clean_merge_of_dev_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch, _docs_only())

    assert check(_staged_commit(repo), "dev", TITLE) == ()


def test_golden_chain_a_committed_merge_of_dev_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch, _docs_only())
    _git(repo, "commit", "-qm", "merge dev")

    assert check("HEAD", "dev", TITLE) == ()


def test_error_chain_a_staged_merge_still_refuses_the_pull_requests_own_umbrella_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = {
        **_docs_only(),
        f"contracts/{UMBRELLA}.yaml": _umbrella("dod-old", "dod-own"),
    }
    repo = _repo(tmp_path, monkeypatch, own)
    # The merge of dev conflicts on the umbrella contract; resolve it as a lane would,
    # keeping dev's entry and the pull request's own.
    (repo / f"contracts/{UMBRELLA}.yaml").write_text(
        _umbrella("dod-old", "dod-own", "dod-landed")
    )
    _git(repo, "add", ".")

    failures = check(_staged_commit(repo), "dev", TITLE)

    assert len(failures) == 1
    assert "dod-own" in failures[0]
    assert "dod-landed" not in failures[0]


def test_error_chain_a_staged_merge_still_refuses_the_pull_requests_own_runtime_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(
        tmp_path, monkeypatch, {**_docs_only(), "src/own_runtime.py": "y = 2\n"}
    )

    failures = check(_staged_commit(repo), "dev", TITLE)

    assert len(failures) == 1
    assert "needs an integration proof" in failures[0]
    assert "src/own_runtime.py" in failures[0]
    assert "src/dev_runtime.py" not in failures[0]
