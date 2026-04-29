"""Root-safety tests for baseline capture probes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
    ModelBaselineCaptureRequest,
)
from omnimarket.nodes.node_baseline_capture.handlers.probes import (
    probe_git_branches,
    probe_github_prs,
)
from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    ModelBaselineCompareRequest,
)


def test_baseline_capture_default_omni_home_comes_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_HOME", "/tmp/omni-home-env")

    request = ModelBaselineCaptureRequest(baseline_id="root-safe", probes=[])

    assert request.omni_home == "/tmp/omni-home-env"


def test_baseline_capture_default_omni_home_fails_without_env(monkeypatch) -> None:
    monkeypatch.delenv("OMNI_HOME", raising=False)

    with pytest.raises(KeyError, match="OMNI_HOME"):
        ModelBaselineCaptureRequest(baseline_id="root-safe", probes=[])


def test_baseline_compare_default_omni_home_comes_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OMNI_HOME", "/tmp/omni-home-env")

    request = ModelBaselineCompareRequest(baseline_id="root-safe")

    assert request.omni_home == "/tmp/omni-home-env"


@pytest.mark.asyncio
async def test_git_branch_probe_uses_omni_home_worktrees(
    monkeypatch,
    tmp_path: Path,
) -> None:
    omni_home = tmp_path / "omni_home"
    repo_dir = omni_home / "omni_worktrees" / "OMN-1" / "omnimarket"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").write_text("gitdir: /tmp/gitdir\n", encoding="utf-8")
    monkeypatch.setattr(probe_git_branches, "_get_current_branch", lambda _path: "x")
    monkeypatch.setattr(probe_git_branches, "_get_branch_age_days", lambda _path: 1.0)

    result = await probe_git_branches.ProbeGitBranches().collect(str(omni_home))

    assert len(result) == 1
    assert result[0].repo == "omnimarket"
    assert result[0].worktree_path == str(repo_dir)


@pytest.mark.asyncio
async def test_github_pr_probe_omits_omninode_infra_by_default(monkeypatch) -> None:
    requested_repos: list[str] = []

    def fake_run(args, **_kwargs):
        requested_repos.append(args[args.index("--repo") + 1])
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    await probe_github_prs.ProbeGitHubPRs().collect("/tmp/omni_home")

    assert "OmniNode-ai/omninode_infra" not in requested_repos
    assert "OmniNode-ai/omnimarket" in requested_repos


def test_github_pr_probe_rejects_bare_string_repo_allowlist() -> None:
    with pytest.raises(TypeError, match="sequence of repo names"):
        probe_github_prs.ProbeGitHubPRs("OmniNode-ai/omnimarket")
