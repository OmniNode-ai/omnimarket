"""Golden-chain tests for node_report_anchor_probe_effect (OMN-15164).

Sha/path probes run real git subprocess calls against throwaway git fixture
repos under tmp_path (never the invoking worktree) -- every git subprocess
call in this file passes an explicit env= that has dropped GIT_DIR /
GIT_WORK_TREE / GIT_INDEX_FILE / GIT_COMMON_DIR / GIT_OBJECT_DIRECTORY /
GIT_ALTERNATE_OBJECT_DIRECTORIES / GIT_CEILING_DIRECTORIES, per
omnibase_core's no_unguarded_git_subprocess pattern (OMN-14891): those
variables override both -C and cwd, so an unscrubbed call under a pre-push
git hook would silently retarget the real invoking worktree instead of
tmp_path. The PR probe is subprocess-mocked (gh, not git) -- same pattern
node_pr_snapshot_effect's golden-chain test already uses for gh.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_report_anchor_probe_effect.handlers.handler_report_anchor_probe import (
    HandlerReportAnchorProbe,
)
from omnimarket.nodes.node_report_anchor_probe_effect.models import (
    EnumAnchorProbeStatus,
    ModelPathAnchorClaim,
    ModelPrAnchorClaim,
    ModelReportAnchorProbeRequest,
    ModelShaAnchorClaim,
)

# ---------------------------------------------------------------------------
# throwaway git fixture-repo helpers (env-scrubbed subprocess, tmp_path only)
# ---------------------------------------------------------------------------

_GIT_LOCATION_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
)


def _scrubbed_git_env() -> dict[str, str]:
    """Drop every inherited git-location override before any git subprocess call."""
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("GIT_CONFIG"):
            del env[key]
    for key in _GIT_LOCATION_ENV_VARS:
        env.pop(key, None)
    return env


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_scrubbed_git_env(),
    )


def _init_git_repo(repo_dir: Path) -> str:
    """Init a throwaway repo with one commit; return its HEAD sha."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@test.test")
    _git(repo_dir, "config", "user.name", "Test")
    (repo_dir / "README.md").write_text("hello\n")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-q", "-m", "init")
    return _git(repo_dir, "rev-parse", "HEAD").stdout.strip()


def _blob_sha(repo_dir: Path, content: str) -> str:
    """Write a blob object (not a commit) and return its sha."""
    result = subprocess.run(
        ["git", "-C", str(repo_dir), "hash-object", "-w", "--stdin"],
        input=content,
        check=True,
        capture_output=True,
        text=True,
        env=_scrubbed_git_env(),
    )
    return result.stdout.strip()


@pytest.fixture
def git_fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repo_dir = tmp_path / "fixture_repo"
    head_sha = _init_git_repo(repo_dir)
    return repo_dir, head_sha


def _handler() -> HandlerReportAnchorProbe:
    return HandlerReportAnchorProbe()


# ---------------------------------------------------------------------------
# sha probes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sha_resolved_green(git_fixture_repo: tuple[Path, str]) -> None:
    repo_dir, head_sha = git_fixture_repo
    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            sha_claims=(ModelShaAnchorClaim(field_name="head_sha", sha=head_sha),),
            git_dir=str(repo_dir / ".git"),
        )
    )
    assert len(result.sha_results) == 1
    probe = result.sha_results[0]
    assert probe.status is EnumAnchorProbeStatus.RESOLVED
    assert probe.resolved is True
    assert result.all_resolved is True


@pytest.mark.unit
def test_sha_missing_context_red() -> None:
    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            sha_claims=(ModelShaAnchorClaim(field_name="head_sha", sha="deadbeef"),),
            git_dir=None,
        )
    )
    probe = result.sha_results[0]
    assert probe.status is EnumAnchorProbeStatus.MISSING_CONTEXT
    assert probe.resolved is False
    assert result.all_resolved is False


@pytest.mark.unit
def test_sha_unresolvable_red(git_fixture_repo: tuple[Path, str]) -> None:
    repo_dir, _head_sha = git_fixture_repo
    handler = _handler()
    fake_sha = "0" * 40
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            sha_claims=(ModelShaAnchorClaim(field_name="verified_sha", sha=fake_sha),),
            git_dir=str(repo_dir / ".git"),
        )
    )
    probe = result.sha_results[0]
    assert probe.status is EnumAnchorProbeStatus.NOT_RESOLVED
    assert probe.resolved is False
    assert result.all_resolved is False


@pytest.mark.unit
def test_sha_blob_not_commit_red(git_fixture_repo: tuple[Path, str]) -> None:
    repo_dir, _head_sha = git_fixture_repo
    blob_sha = _blob_sha(repo_dir, "not a commit")
    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            sha_claims=(ModelShaAnchorClaim(field_name="merge_sha", sha=blob_sha),),
            git_dir=str(repo_dir / ".git"),
        )
    )
    probe = result.sha_results[0]
    # cat-file -e <sha>^{commit} fails for a blob even though the object
    # itself exists -- this is exactly the peel-requirement the ^{commit}
    # suffix exists to enforce.
    assert probe.status is EnumAnchorProbeStatus.NOT_RESOLVED
    assert result.all_resolved is False


@pytest.mark.unit
def test_sha_git_dir_does_not_exist_red(tmp_path: Path) -> None:
    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            sha_claims=(ModelShaAnchorClaim(field_name="head_sha", sha="deadbeef"),),
            git_dir=str(tmp_path / "does-not-exist" / ".git"),
        )
    )
    probe = result.sha_results[0]
    assert probe.status is EnumAnchorProbeStatus.MISSING_CONTEXT


# ---------------------------------------------------------------------------
# path probes
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_path_resolved_green(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "evidence").mkdir(parents=True)
    artifact = repo_root / "docs" / "evidence" / "report.md"
    artifact.write_text("evidence\n")

    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(
                ModelPathAnchorClaim(
                    field_name="evidence_paths", path="docs/evidence/report.md"
                ),
            ),
            repo_root=str(repo_root),
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.RESOLVED
    assert probe.resolved is True
    assert probe.resolved_path == str(artifact.resolve())
    assert result.all_resolved is True


@pytest.mark.unit
def test_path_missing_context_red() -> None:
    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(
                ModelPathAnchorClaim(field_name="evidence_paths", path="report.md"),
            ),
            repo_root=None,
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.MISSING_CONTEXT
    assert result.all_resolved is False


@pytest.mark.unit
def test_path_missing_file_red(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(
                ModelPathAnchorClaim(
                    field_name="evidence_paths", path="does/not/exist.md"
                ),
            ),
            repo_root=str(repo_root),
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.NOT_FOUND
    assert result.all_resolved is False


@pytest.mark.unit
def test_path_escaping_repo_root_red(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")

    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(
                ModelPathAnchorClaim(
                    field_name="evidence_paths", path="../outside.txt"
                ),
            ),
            repo_root=str(repo_root),
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.ESCAPES_ROOT
    assert result.all_resolved is False


@pytest.mark.unit
def test_path_absolute_escape_red(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(
                ModelPathAnchorClaim(field_name="evidence_paths", path="/etc/hosts"),
            ),
            repo_root=str(repo_root),
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.ESCAPES_ROOT


@pytest.mark.unit
def test_path_dir_not_file_red(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "docs").mkdir(parents=True)

    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(
                ModelPathAnchorClaim(field_name="evidence_paths", path="docs"),
            ),
            repo_root=str(repo_root),
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.NOT_A_FILE
    assert result.all_resolved is False


@pytest.mark.unit
def test_path_repo_root_itself_not_a_file_red(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    handler = _handler()
    result = handler.handle(
        ModelReportAnchorProbeRequest(
            correlation_id=uuid4(),
            path_claims=(ModelPathAnchorClaim(field_name="evidence_paths", path="."),),
            repo_root=str(repo_root),
        )
    )
    probe = result.path_results[0]
    assert probe.status is EnumAnchorProbeStatus.NOT_A_FILE


# ---------------------------------------------------------------------------
# pr probe (gh subprocess-mocked, matching node_pr_snapshot_effect's pattern)
# ---------------------------------------------------------------------------


def _mock_gh_result(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> MagicMock:
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = stdout
    mock_result.stderr = stderr
    return mock_result


@pytest.mark.unit
def test_pr_confirmed_green() -> None:
    handler = _handler()
    with patch(
        "subprocess.run",
        return_value=_mock_gh_result(stdout='{"number": 213, "state": "MERGED"}'),
    ):
        result = handler.handle(
            ModelReportAnchorProbeRequest(
                correlation_id=uuid4(),
                pr_claim=ModelPrAnchorClaim(
                    field_name="pr_number",
                    pr_number=213,
                    repo="owner/repo",
                ),
            )
        )
    assert result.pr_result is not None
    assert result.pr_result.status is EnumAnchorProbeStatus.RESOLVED
    assert result.pr_result.state == "MERGED"
    assert result.all_resolved is True


@pytest.mark.unit
def test_pr_not_found_red() -> None:
    handler = _handler()
    with patch(
        "subprocess.run",
        return_value=_mock_gh_result(returncode=1, stderr="no pull requests found"),
    ):
        result = handler.handle(
            ModelReportAnchorProbeRequest(
                correlation_id=uuid4(),
                pr_claim=ModelPrAnchorClaim(
                    field_name="pr_number",
                    pr_number=999999,
                    repo="owner/repo",
                ),
            )
        )
    assert result.pr_result is not None
    assert result.pr_result.status is EnumAnchorProbeStatus.NOT_FOUND
    assert result.all_resolved is False


@pytest.mark.unit
def test_pr_lookup_failed_on_timeout_red() -> None:
    handler = _handler()
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired("gh", 30),
    ):
        result = handler.handle(
            ModelReportAnchorProbeRequest(
                correlation_id=uuid4(),
                pr_claim=ModelPrAnchorClaim(
                    field_name="pr_number", pr_number=213, repo="owner/repo"
                ),
            )
        )
    assert result.pr_result is not None
    assert result.pr_result.status is EnumAnchorProbeStatus.LOOKUP_FAILED
    assert result.all_resolved is False


@pytest.mark.unit
def test_no_pr_claim_yields_none() -> None:
    handler = _handler()
    result = handler.handle(ModelReportAnchorProbeRequest(correlation_id=uuid4()))
    assert result.pr_result is None
    # vacuous true: zero claims total means nothing failed
    assert result.all_resolved is True
    assert result.total_claims == 0


# ---------------------------------------------------------------------------
# aggregate / full-request golden chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_full_request_all_green(git_fixture_repo: tuple[Path, str]) -> None:
    repo_dir, head_sha = git_fixture_repo
    (repo_dir / "docs").mkdir()
    (repo_dir / "docs" / "evidence.md").write_text("evidence\n")

    handler = _handler()
    real_run: Any = subprocess.run

    def _side_effect(*args: Any, **kwargs: Any) -> Any:
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv and argv[0] == "gh":
            return _mock_gh_result(stdout='{"number": 1, "state": "OPEN"}')
        return real_run(*args, **kwargs)

    with patch("subprocess.run", side_effect=_side_effect):
        result = handler.handle(
            ModelReportAnchorProbeRequest(
                correlation_id=uuid4(),
                sha_claims=(ModelShaAnchorClaim(field_name="head_sha", sha=head_sha),),
                path_claims=(
                    ModelPathAnchorClaim(
                        field_name="evidence_paths", path="docs/evidence.md"
                    ),
                ),
                pr_claim=ModelPrAnchorClaim(
                    field_name="pr_number", pr_number=1, repo="owner/repo"
                ),
                git_dir=str(repo_dir / ".git"),
                repo_root=str(repo_dir),
            )
        )

    assert result.total_claims == 3
    assert result.all_resolved is True
    assert result.sha_results[0].status is EnumAnchorProbeStatus.RESOLVED
    assert result.path_results[0].status is EnumAnchorProbeStatus.RESOLVED
    assert result.pr_result is not None
    assert result.pr_result.status is EnumAnchorProbeStatus.RESOLVED
