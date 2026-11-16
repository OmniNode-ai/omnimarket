# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay a landing worker's missing origin/dev through the installed hook adapter.

The recorded worker on 2026-11-01 returned 128 resolving origin/dev^{commit}.
A single-branch clone reproduces that state with a real local git remote.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit
TICKET = "OMN-20864"
ROOT = Path(__file__).resolve().parents[3]
MODULE = "omnimarket.nodes.node_ticket_contract_prerequisite_effect"


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "PYTHONPATH": str(ROOT / "src"),
    }


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=scrub_git_location_env(_env()),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _remote_branches(repo: Path) -> list[str]:
    """Remote-tracking branches by full ref name, never ``origin/HEAD``.

    ``git branch -r`` prints ``origin/HEAD -> origin/worker`` after a single-branch
    clone on some git versions (2.43 prints it, 2.56 does not), so the text of that
    command is not a stable fact. ``origin/HEAD`` is a symbolic pointer, not a
    branch, and is left out; every real branch ref is still listed.
    """
    refs = _git(
        repo, "for-each-ref", "--format=%(refname)", "refs/remotes"
    ).splitlines()
    return sorted(ref for ref in refs if not ref.endswith("/HEAD"))


def _worker(tmp_path: Path, *, contract: bool = True, runtime: bool = False) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "dev")
    (origin / "README.md").write_text("base\n")
    _git(origin, "add", "README.md")
    _git(origin, "commit", "-q", "-m", "base")
    _git(origin, "switch", "-q", "-c", "worker")
    if contract:
        (origin / "contracts").mkdir()
        (origin / "contracts" / f"{TICKET}.yaml").write_text(
            yaml.safe_dump(
                {
                    "ticket_id": TICKET,
                    "requirements": [
                        {"acceptance": [{"id": "AC1", "statement": "check"}]}
                    ],
                    "dod_evidence": [
                        {
                            "id": "unit",
                            "binds_ac": ["AC1"],
                            "checks": [
                                {
                                    "check_type": "test_passes",
                                    "check_value": "uv run pytest tests/test_x.py -q",
                                }
                            ],
                        }
                    ],
                }
            )
        )
    changed = origin / ("src/change.py" if runtime else "docs/change.md")
    changed.parent.mkdir()
    changed.write_text("change\n")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", f"fix({TICKET}): change")
    worker = tmp_path / "worker"
    _git(
        tmp_path,
        "clone",
        "-q",
        "--single-branch",
        "-b",
        "worker",
        str(origin),
        str(worker),
    )
    assert _remote_branches(worker) == ["refs/remotes/origin/worker"]
    return worker


def _hook(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", MODULE, *args],
        cwd=repo,
        env=scrub_git_location_env(_env()),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("explicit_title", [False, True])
def test_missing_remote_base_is_fetched_before_checking(
    tmp_path: Path, explicit_title: bool
) -> None:
    worker = _worker(tmp_path)
    before = _git(worker, "rev-parse", "HEAD")
    title = ("--title", f"fix({TICKET}): change") if explicit_title else ()
    result = _hook(worker, *title)
    assert result.returncode == 0, result.stderr
    assert _git(worker, "rev-parse", "origin/dev") == _git(
        tmp_path / "origin", "rev-parse", "dev"
    )
    assert _git(worker, "rev-parse", "HEAD") == before
    assert _git(worker, "status", "--porcelain") == ""


def test_fetch_preserves_missing_contract_refusal(tmp_path: Path) -> None:
    worker = _worker(tmp_path, contract=False)
    result = _hook(worker)
    assert result.returncode == 1
    assert (
        result.stderr.strip()
        == f"pull request cites {TICKET} but carries no contracts/{TICKET}.yaml"
    )
    assert _git(worker, "rev-parse", "origin/dev")


# A base that names a remote and cannot be fetched propagates its fetch error to the
# failed typed verdict (the handler's documented behaviour on dev), so only a base
# that names no remote keeps the contract refusal.
@pytest.mark.parametrize("base", ["no-such-base"])
def test_unreadable_base_preserves_explicit_missing_contract_refusal(
    tmp_path: Path, base: str
) -> None:
    """The CLI must retain the contract refusal even when base repair fails."""
    worker = _worker(tmp_path, contract=False)
    before = _git(worker, "rev-parse", "HEAD")
    _git(worker, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    result = _hook(worker, "--base", base, "--title", f"fix({TICKET}): change")
    assert result.returncode == 1
    assert result.stderr.strip() == (
        f"pull request cites {TICKET} but carries no contracts/{TICKET}.yaml"
    )
    assert _git(worker, "rev-parse", "HEAD") == before
    assert _git(worker, "status", "--porcelain") == ""
    assert _remote_branches(worker) == ["refs/remotes/origin/worker"]


def test_fetch_preserves_runtime_integration_proof_refusal(tmp_path: Path) -> None:
    worker = _worker(tmp_path, runtime=True)
    result = _hook(worker)
    assert result.returncode == 1
    assert "needs an integration proof" in result.stderr
    assert "src/change.py" in result.stderr
    assert _git(worker, "rev-parse", "origin/dev")


def test_fetch_failure_refuses_instead_of_assuming_empty_diff(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    _git(worker, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    result = _hook(worker)
    assert result.returncode == 1
    assert "ticket contract preflight could not read" in result.stderr
    assert "'fetch'" in result.stderr


def test_present_base_needs_no_remote_connection(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    base = _git(tmp_path / "origin", "rev-parse", "dev")
    _git(worker, "update-ref", "refs/remotes/origin/dev", base)
    _git(worker, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    result = _hook(worker)
    assert result.returncode == 0, result.stderr


def test_invalid_outgoing_head_does_not_fetch(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    result = _hook(worker, "--head", "not-a-commit")
    assert result.returncode == 1
    assert _remote_branches(worker) == ["refs/remotes/origin/worker"]


@pytest.mark.parametrize("base", ["refs/remotes/origin/dev", "upstream/dev"])
def test_explicit_remote_base_uses_its_exact_tracking_ref(
    tmp_path: Path, base: str
) -> None:
    worker = _worker(tmp_path)
    if base.startswith("upstream/"):
        _git(worker, "remote", "add", "upstream", str(tmp_path / "origin"))
        _git(worker, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    result = _hook(worker, "--base", base)
    assert result.returncode == 0, result.stderr
    assert _git(worker, "rev-parse", base) == _git(
        tmp_path / "origin", "rev-parse", "dev"
    )


@pytest.mark.parametrize("explicit_title", [False, True])
def test_missing_local_base_is_not_guessed_into_a_remote(
    tmp_path: Path, explicit_title: bool
) -> None:
    worker = _worker(tmp_path)
    title = ("--title", f"fix({TICKET}): change") if explicit_title else ()
    result = _hook(worker, "--base", "dev", *title)
    assert result.returncode == 1
    assert "'fetch'" not in result.stderr
    assert _remote_branches(worker) == ["refs/remotes/origin/worker"]


def test_unreadable_local_base_keeps_the_missing_contract_refusal(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path, contract=False)
    result = _hook(worker, "--base", "dev", "--title", f"fix({TICKET}): change")
    assert result.returncode == 1
    assert (
        result.stderr.strip()
        == f"pull request cites {TICKET} but carries no contracts/{TICKET}.yaml"
    )
    assert "could not read" not in result.stderr
    assert _remote_branches(worker) == ["refs/remotes/origin/worker"]


def test_remote_branch_list_ignores_origin_head_and_keeps_real_branches(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    _git(worker, "remote", "set-head", "origin", "worker")
    assert "origin/HEAD" in _git(worker, "branch", "-r")
    assert _remote_branches(worker) == ["refs/remotes/origin/worker"]
    base = _git(tmp_path / "origin", "rev-parse", "dev")
    _git(worker, "update-ref", "refs/remotes/origin/dev", base)
    assert _remote_branches(worker) == [
        "refs/remotes/origin/dev",
        "refs/remotes/origin/worker",
    ]


def test_missing_remote_base_is_fetched_with_origin_head_present(
    tmp_path: Path,
) -> None:
    worker = _worker(tmp_path)
    _git(worker, "remote", "set-head", "origin", "worker")
    result = _hook(worker)
    assert result.returncode == 0, result.stderr
    assert _git(worker, "rev-parse", "origin/dev") == _git(
        tmp_path / "origin", "rev-parse", "dev"
    )


def test_unresolvable_hook_run_ref_falls_back_to_head(tmp_path: Path) -> None:
    """A hook-run ref from another worktree does not end the lookup at exit 128 (OMN-23283)."""
    worker = _worker(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", MODULE],
        cwd=worker,
        env=scrub_git_location_env(
            {**_env(), "PRE_COMMIT_TO_REF": "0" * 40, "PRE_COMMIT_FROM_REF": "0" * 40}
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
